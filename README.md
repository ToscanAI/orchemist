# Orchemist

### Like Docker Compose for AI pipelines — define phases in YAML, the engine handles the rest.

[![CI](https://github.com/ToscanAI/orchemist/actions/workflows/ci.yml/badge.svg)](https://github.com/ToscanAI/orchemist/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/orchemist)](https://pypi.org/project/orchemist/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)


---

## What Is It?

**Orchemist** is a YAML-first orchestration engine for multi-agent AI pipelines.

You declare your pipeline — phases, dependencies, model tiers, and acceptance criteria — in a single YAML file. The engine handles phase sequencing, dependency resolution, output forwarding, automatic retries, fallback executors, and scenario grading. No boilerplate. Works standalone with the Anthropic API or via OpenClaw sub-agent spawning. Git-native pipeline handoff for reliable multi-phase execution.

> **Note:** The YAML below is simplified for illustration. Orchemist's template format is evolving to support an expanding range of workloads — from content pipelines to coding, research, compliance, and beyond. See [Template Authoring](docs/template-authoring.md) for the full schema and working examples.

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
pip install orchemist
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
| **Coding Pipeline** | Feature development → Unit tests → Integration tests → Deployment |
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

| Feature | Orchemist | LangGraph | CrewAI | Autogen | Dify |
|---------|:-------------------:|:---------:|:------:|:-------:|:----:|
| YAML-first | ✅ | ❌ | ❌ | ❌ | Partial |
| Visual builder | 🔜 | ⚠️ | ❌ | ❌ | ✅ |
| Template library | ✅ | ❌ | ❌ | ❌ | ✅ |
| Testing / grading | ✅ | ❌ | ⚠️ | ❌ | ❌ |
| Raspberry Pi support | ✅ | ⚠️ | ⚠️ | ⚠️ | ❌ |

> ✅ full support · ⚠️ partial/unofficial · ❌ not supported · 🔜 planned

---

## Architecture

```mermaid
flowchart TD
    subgraph Templates["YAML Templates"]
        T1["content-pipeline.yaml"]
        T2["code-review.yaml"]
        T3["…"]
    end

    Templates -- "orch run" --> Runner

    subgraph Runner["Pipeline Runner"]
        TE["Template Engine\n(YAML parse, var interp)"] --> PS["Phase Sequencer\n(topo sort, output forward,\nretry logic)"]
        PS --> EX["Executors\n(Anthropic · OpenClaw\nGemini · Dry-Run)"]
    end

    Runner --> SR

    subgraph SR["Scenario Runner (optional acceptance testing)"]
        AG["Assertion Grader"] ~~~ LJ["LLM Judge"] ~~~ UC["URL Check"]
    end
```

**Three execution modes:**

| Mode | How it runs | API key? | Start here? |
|------|-------------|----------|-------------|
| `standalone` | Direct Anthropic API (zero framework deps) | Yes — `ANTHROPIC_API_KEY` | ✅ Yes — simplest setup |
| `dry-run` | Mock executor for testing / CI | No | ✅ Yes — no credentials needed |
| `openclaw` | Sub-agent spawning via OpenClaw gateway | No (uses gateway token) | Requires separate [OpenClaw](https://github.com/openclaw/openclaw) setup |

> **New here?** Start with `--mode standalone` (bring your own API key) or `--mode dry-run` (no credentials). OpenClaw mode is for production deployments with the OpenClaw gateway.

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
pip install orchemist
```

### From Source

```bash
# Development repo (latest)
git clone https://github.com/ToscanAI/orchestration-engine.git
# Or stable releases:
# git clone https://github.com/connylazo/orchestration-engine.git
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
| **Orchemist** | Pipeline templates, phase sequencing, quality gates | Application Framework |
| **Scenario Runner** | Outcome-based testing, LLM judges, grading | Test Framework |

The engine works **standalone** (direct API) or **with OpenClaw** (sub-agent spawning). No vendor lock-in on the model provider side.

---

## Git as Runtime Dependency

Orchemist uses **git commits as the source of truth** for multi-phase pipeline handoff. Each pipeline phase commits its output, and the next phase reads from a specific commit hash — making stale reads structurally impossible and providing a full audit trail of every pipeline run.

**What this means in practice:**

- **Pipeline execution** (coding pipelines, spec loops) requires a git repository
- **Dry-run mode** does NOT require git — works anywhere
- **Standalone mode** with simple linear pipelines works without git
- **Git-based features:** commit-based phase handoff, diff-based adversary review, immutable test references

This is a deliberate architectural choice: git provides immutability, diffing, and audit trails that would otherwise require custom infrastructure. The trade-off is that production pipeline execution is coupled to git — which is already a prerequisite for coding pipelines (branch creation, commits, PRs).

> **Future:** Pluggable VCS backends are on the roadmap but not prioritized for initial release. If you need non-git support, open an issue.

---

## Contributing

Pull requests are welcome! Here's how to get started:

```bash
git clone https://github.com/ToscanAI/orchestration-engine.git
cd orchestration-engine
pip install -e ".[dev]"
pytest
```

**Areas where contributions are especially welcome:**

- 📦 **Community templates** — add a YAML template to `templates/` and submit a PR
- 🧪 **Scenarios & rubrics** — improve grading quality for existing templates
- 🔌 **Executors** — add support for new model providers (Gemini, Mistral, local models)
- 📖 **Documentation** — improve examples, add tutorials, translate docs

**Filing issues:** Use the right template for the right pipeline:

| Issue type | Template | Pipeline |
|------------|----------|----------|
| Bug | Bug Report | `coding-pipeline-v1` |
| Feature / code change | Feature Request | `coding-pipeline-v1` |
| Documentation | Documentation Request | `docs-pipeline-v1` |
| Article / blog post | Content Request | `content-pipeline` |
| Research / analysis | Research Request | `research-competitive` |

Please read CONTRIBUTING.md for code style and PR guidelines.

---

## Documentation

- [Getting Started](docs/GETTING_STARTED.md) — detailed setup guide
- [Architecture](docs/ARCHITECTURE.md) — system design
- [API Reference](docs/api-reference.md) — CLI commands + Python classes
- [Web UI](docs/web-ui.md) — browser interface (`orch serve`)
- [Tech Stack](docs/tech-stack.md) — dependencies and choices
- [Security Policy](SECURITY.md) — vulnerability reporting & supported versions

---

## License

MIT © [Conny Lazo](https://connylazo.com) & [Toscan](https://github.com/ToscanAI)

See [LICENSE](LICENSE) for the full text.

---

**Orchemist — Tests passing. 3 execution modes. Git-native pipeline handoff.**
