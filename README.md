# Orchestration Engine

### Like Docker Compose for AI pipelines — define phases in YAML, the engine handles the rest.

[![Tests](https://img.shields.io/badge/tests-passing-brightgreen)](https://github.com/ToscanRivera/orchestration-engine/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Pi Tested](https://img.shields.io/badge/Raspberry%20Pi-tested-red)](https://www.raspberrypi.com/)

---

## What Is It?

**Orchestration Engine** is a YAML-first orchestration engine for multi-agent AI pipelines.

You declare your pipeline — phases, dependencies, model tiers, and acceptance criteria — in a single YAML file. The engine handles phase sequencing, dependency resolution, output forwarding, automatic retries, fallback executors, and scenario grading. No boilerplate. No vendor lock-in. Works standalone with the Anthropic API or via OpenClaw sub-agent spawning.

```yaml
name: content-pipeline
phases:
  research:
    prompt: "Research the topic: {{brief}}"
    model_tier: haiku

  draft:
    prompt: "Write a 500-word article based on: {{research.output}}"
    model_tier: sonnet
    depends_on: [research]

  edit:
    prompt: "Polish and improve this draft: {{draft.output}}"
    model_tier: sonnet
    depends_on: [draft]
```

---

## Quickstart

```bash
pip install orchestration-engine
orch new --yes --output templates/my-pipeline.yaml
orch run templates/my-pipeline.yaml --mode dry-run
```

No API key needed for a dry run:

```bash
orch run templates/my-pipeline.yaml --mode dry-run --input '{"brief": "AI safety"}'
```

Live run against Claude:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
orch run templates/my-pipeline.yaml --mode standalone --input '{"brief": "AI safety"}'
```

---

## Use Cases

| Use Case | What it does |
|----------|-------------|
| **Content Pipeline** | Research → Draft → Edit → SEO → Publish-ready output |
| **Code Review** | Static analysis → Security scan → Architecture review → Summary report |
| **Research Assistant** | Query expansion → Source gathering → Synthesis → Citation check |
| **Translation Pipeline** | Translate → Back-translate → Consistency check → Final polish |
| **Customer Support** | Intent classification → KB lookup → Response draft → Quality gate |
| **Financial Analysis** | Data extraction → Trend analysis → Risk assessment → Executive summary |

Each use case is a template. Browse them with `orch templates list` or search with `orch templates search <topic>`.

---

## Features

- ✅ **YAML-first pipeline definitions** — version-controlled, diff-friendly, no code required
- ✅ **Phase sequencing with dependency graphs** — topological sort, parallel wave execution
- ✅ **Model tier selection per phase** — haiku / sonnet / opus, set per phase or pipeline-wide
- ✅ **`skill_refs` injection** — pass tool contexts into prompts declaratively
- ✅ **Fallback executors** — Gemini fallback when Anthropic is unavailable
- ✅ **Template index & search** — community index, install by GitHub shorthand `user/repo`
- ✅ **Scenario-based grading** — YAML acceptance criteria, LLM judges, assertion graders
- ✅ **Human-in-the-loop** — pause phases for review, inject feedback, resume
- ✅ **OpenClaw integration** — run phases as sub-agents with full tool access
- ✅ **Local web UI** — browse templates, start runs, and watch live progress in your browser (`orch serve`)

---

## How It Compares

| Feature | Orchestration Engine | LangGraph | CrewAI | Autogen | Dify |
|---------|:-------------------:|:---------:|:------:|:-------:|:----:|
| YAML-first | ✅ | ❌ | ❌ | ❌ | Partial |
| Visual builder | 🔜 | ⚠️ | ❌ | ❌ | ✅ |
| Template library | ✅ | ❌ | ❌ | ❌ | ✅ |
| Testing / grading | ✅ | ❌ | ⚠️ | ❌ | ❌ |
| Raspberry Pi support | ✅ | ⚠️ | ⚠️ | ⚠️ | ❌ |

> ✅ full support · ⚠️ partial/unofficial · ❌ not supported · 🔜 planned

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  YAML Templates   (community index / local / GitHub)        │
│  content-pipeline.yaml  ·  code-review.yaml  ·  …          │
└────────────────────────┬────────────────────────────────────┘
                         │  orch run
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Pipeline Runner                                            │
│  ┌─────────────┐   ┌──────────────────┐   ┌─────────────┐  │
│  │ Template    │ → │ Phase Sequencer  │ → │  Executors  │  │
│  │ Engine      │   │ (topo sort,      │   │             │  │
│  │ (YAML parse │   │  output forward, │   │ Anthropic   │  │
│  │  var interp)│   │  retry logic)    │   │ OpenClaw    │  │
│  └─────────────┘   └──────────────────┘   │ Gemini      │  │
│                                           │ Dry-Run     │  │
│                                           └─────────────┘  │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Scenario Runner  (optional acceptance testing)             │
│  Assertion Grader · LLM Judge · URL Check                   │
└─────────────────────────────────────────────────────────────┘
```

**Three execution modes:**

| Mode | How it runs | API key? |
|------|-------------|----------|
| `standalone` | Direct Anthropic API (zero framework deps) | Yes |
| `openclaw` | Sub-agent spawning via OpenClaw gateway | No |
| `dry-run` | Mock executor for testing / CI | No |

---

## CLI Reference

```bash
# Create a new pipeline template (interactive wizard)
orch new

# Non-interactive scaffold with defaults
orch new --yes --output ./templates/my-pipeline.yaml

# Clone an existing template as a starting point
orch new --from content-pipeline

# Interactive wizard (config_schema-driven)
orch start

# Copy a starter pipeline to your project in one command
orch quickstart

# Execute a pipeline
orch run <template-or-file> --mode standalone --input '{"brief": "..."}'
orch run <template-or-file> --mode dry-run
orch run <template-or-file> --mode openclaw

# Validate a template (checks YAML syntax + structural rules)
orch validate <template-or-file>
orch validate <template-or-file> --fix    # auto-correct simple issues

# Show execution order and model tiers
orch list-phases <template-or-file>

# Browse templates
orch templates list
orch templates info <name>
orch templates search <query>

# Install / remove templates
orch templates install user/repo          # GitHub shorthand
orch templates install https://github.com/user/repo
orch templates install ./my-template.yaml --name my-pipeline
orch templates uninstall <name>

# Task queue (for async / long-running workflows)
orch submit --type <type> --payload '{"key": "value"}'
orch status [task-id]
orch list [--state running] [--type llm_call]
orch cancel <task-id>
orch retry <task-id>
orch watch <task-id> --follow
orch health
```

---

## Installation

### From PyPI

```bash
pip install orchestration-engine
```

### From Source

```bash
git clone https://github.com/ToscanRivera/orchestration-engine.git
cd orchestration-engine
python3 -m venv .venv && source .venv/bin/activate
pip install .
```

### Verify

```bash
orch --help
```

---

## Relationship to OpenClaw

| Layer | Provides | Think of it as… |
|-------|----------|-----------------|
| **OpenClaw** | Sub-agent spawning, tool access, model switching | Operating System |
| **Orchestration Engine** | Pipeline templates, phase sequencing, quality gates | Application Framework |
| **Scenario Runner** | Outcome-based testing, LLM judges, grading | Test Framework |

The engine works **standalone** (direct API) or **with OpenClaw** (sub-agent spawning). No vendor lock-in.

---

## Contributing

Pull requests are welcome! Here's how to get started:

```bash
git clone https://github.com/ToscanRivera/orchestration-engine.git
cd orchestration-engine
pip install -e ".[test]"
pytest
```

**Areas where contributions are especially welcome:**

- 📦 **Community templates** — add a YAML template to `templates/` and submit a PR
- 🧪 **Scenarios & rubrics** — improve grading quality for existing templates
- 🔌 **Executors** — add support for new model providers (Gemini, Mistral, local models)
- 📖 **Documentation** — improve examples, add tutorials, translate docs

Please read CONTRIBUTING.md for code style and PR guidelines.

---

## Documentation

- [Getting Started](docs/GETTING_STARTED.md) — detailed setup guide
- [Architecture](docs/ARCHITECTURE.md) — system design
- [API Reference](docs/api-reference.md) — CLI commands + Python classes
- [Web UI](docs/web-ui.md) — browser interface (`orch serve`)
- [Tech Stack](docs/tech-stack.md) — dependencies and choices

---

## License

MIT © [Conny Lazo](https://connylazo.com) & [Toscan](https://github.com/ToscanRivera)

See [LICENSE](LICENSE) for the full text.

---

**Tests passing. 3 execution modes. Zero vendor lock-in.**
