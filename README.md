# Orchemist

### Scenario-driven orchestration for multi-agent AI pipelines.

[![CI](https://github.com/ToscanAI/orchemist/actions/workflows/ci.yml/badge.svg)](https://github.com/ToscanAI/orchemist/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/orchemist)](https://pypi.org/project/orchemist/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)


---

## Why Orchemist?

AI agents hallucinate, drift off-topic across long chains, and produce work that looks correct until you test it. Orchemist solves this with three architectural principles:

- **Ground-truth anchoring.** Every phase prompt is anchored to the original issue body. Agents cannot invent features that aren't in the spec.
- **Adversarial quality gates.** Spec adversary, acceptance-test adversary, and code review phases catch weaknesses BEFORE they become implementation bugs.
- **Acceptance-test-driven development.** Tests are written before implementation by a separate agent. The implementing agent must pass them — it cannot modify or bypass them.

Dogfooded across content, coding, research, and compliance workflows — these architectural choices come from real pipeline runs, not theory.

---

## What Is It?

**Orchemist** is a platform with four components:

1. **Orchestration Engine** (this repo) — a Python engine that sequences multi-phase AI pipelines from YAML templates. Handles phase transitions, retries, tool execution, cost tracking, and adversarial review loops.
2. **Pipeline Templates** (`templates/`) — YAML definitions for coding, content, research, and compliance pipelines. The coding pipeline (`coding-pipeline-standard`) runs 12 phases: spec, behavioral contracts, adversary review, acceptance tests, implementation, test execution, code review, fixes, and final verification.
3. **Skills Pack** ([ToscanAI/orchemist-skills](https://github.com/ToscanAI/orchemist-skills)) — the coding pipeline repackaged as Claude Code `.claude/skills/` and `.claude/agents/`. Pure markdown, no Python runtime. Drop it into Claude Code and `/orchemist:run` walks the same 12-phase loop. Best on-ramp if you already use Claude Code.
4. **Harness** (`frontend/` in this repo) — the operator web surface. Six cross-linked screens covering fleet status, run cockpit, the cross-model adversary dialogue visualizer, trust & gates, admin / activation, and skills-pack mode. Ships with the engine; launches via `orch serve`. See **[The Harness](#the-harness)** below for screenshots.

> The legacy [ToscanAI/orchemist-ide](https://github.com/ToscanAI/orchemist-ide) VS Code fork is being sunset in favour of the harness. See issue [#814](https://github.com/ToscanAI/orchemist/issues/814) for the deprecation plan.

You declare your pipeline in a single YAML file. The engine handles phase sequencing, dependency resolution, output forwarding, automatic retries, tool-calling, and scenario grading.

### Execution modes

| Mode | Backend | Status |
|---|---|---|
| **openrouter** | Any model via OpenRouter (Anthropic, OpenAI, Google, etc.) | Primary — production path since 2026-04-17 |
| **openclaw** | Claude sub-agents via OpenClaw gateway | Deprecated — gateway no longer active; historical use only |
| **standalone** | Direct Anthropic API | Available for simple pipelines |
| **dry-run** | Mock execution for template validation | Stable |

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

### Using OpenRouter (multi-provider)

To route through OpenRouter instead of calling Anthropic directly, export an OpenRouter key and select `--mode openrouter`:

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
orch run templates/my-pipeline.yaml --mode openrouter --input '{"brief": "AI safety"}'
```

`orch serve` captures environment variables at startup — export the key *before* launching the server, and restart it if you rotate the key. See [docs/openrouter-setup.md](docs/openrouter-setup.md) for the full configuration reference, CLI-flag precedence, and troubleshooting.

---

## The Harness

`orch serve` opens the **Orchemist Harness** — a six-screen operator surface designed against the canonical mockups in [`docs/harness-redesign-2026-05-24/screens/`](docs/harness-redesign-2026-05-24/screens). Live screenshots against a real `orch serve` are checked in under [`docs/harness-redesign-2026-05-24/screenshots/live/`](docs/harness-redesign-2026-05-24/screenshots/live).

> **Engine required (v1, #888).** The harness REQUIRES a reachable engine — `orch serve` must be running before pages will render content. The static build still ships, but without an engine the harness short-circuits to an "Engine unreachable at &lt;url&gt;" error UI (with a retry button and a docs link) on every screen. There is **no demo-data fallback** — that v0 ergonomic was misleading because a misconfigured port looked healthy. See issue [#888](https://github.com/ToscanAI/orchemist/issues/888) for the rationale and breaking-change context.

### 1. Fleet Dashboard

Multi-repo status, in-flight pipeline runs, autonomy-level ramp, regression queue, and stale-detection findings — all at a glance.

![Fleet Dashboard](docs/harness-redesign-2026-05-24/screenshots/01-fleet-dashboard.png)

### 2. Run Cockpit

Live phase-by-phase progress for a single pipeline run, with the Phase 0 existing-symbols inventory, sub-check 7d verdict chips (CONSUME / EXTEND / DIVERGENT / NEW-OK), the live tool-call stream, sealed artifact list, and a Jaccard drift indicator.

![Run Cockpit](docs/harness-redesign-2026-05-24/screenshots/02-run-cockpit.png)

### 3. Adversary Loop visualizer

The marquee screen — the cross-model drafter ↔ reviewer dialogue. Each round shows the proposed spec, the reviewer's verdict, and the Jaccard convergence metric. This is the *trust-engine wedge* in operation: a Claude drafter critiqued by a Gemini reviewer (or any other model family) at the spec→implementation boundary, with full per-turn cost ledger.

![Adversary Loop](docs/harness-redesign-2026-05-24/screenshots/03-adversary-loop.png)

### 4. Trust & Gates

Operator queue for pipeline runs awaiting a human decision, with per-row Approve / Reject calls into `/api/v1/gates/{run_id}/(approve|reject)`. Side panel shows per-(repo, template, task) trust profile and a 14-day calibration curve.

![Trust & Gates](docs/harness-redesign-2026-05-24/screenshots/04-trust-gates.png)

### 5. Admin / Activation

The control surface: autonomy ramp (Level 3 → 5), execution-mode toggles (openrouter / standalone / openclaw / dry-run — the harness is model-agnostic by design), branch-protection audit, kill switches, webhook trigger CRUD, and feature flags for the v4.2 pipeline (`phase0_hard_gate`, `EXTEND` verdict, dialogue phase, cross-repo orchestration).

![Admin / Activation](docs/harness-redesign-2026-05-24/screenshots/05-admin-activation.png)

### 6. Skills Pack Mode

Mirror of the local Claude Code Skills Pack installation. Shows phase skills, pipeline YAMLs, local run history, and the `/orchemist:run` invocation preview. Lets operators "promote to remote" — replay a local run via the engine for trust calibration.

![Skills Pack Mode](docs/harness-redesign-2026-05-24/screenshots/06-skills-pack-mode.png)

> **Try it.** `pip install orchemist[web] && orch serve` opens the harness at <http://127.0.0.1:8374>. With the engine running, every screen consumes real `/api/v1/*` data; without a reachable engine, each screen shows the `EngineOfflineGuard` "engine unreachable" error UI (retry button + docs link) — there is no demo-data fallback (see #888). A Playwright e2e suite (`frontend/tests-e2e/`) keeps both the offline-error and live-engine screenshots in sync with the SVG canon on every PR.

---

## Use Cases

| Template | What it does |
|----------|-------------|
| `content-pipeline` | Research → draft → edit content workflow |
| `coding-pipeline-standard` | 12-phase spec → implement → review coding pipeline |
| `coding-pipeline-skip-spec` | Coding pipeline with the spec loop pre-supplied |
| `research-competitive` | Competitive / landscape research |
| `docs-pipeline-v1` | Documentation generation / refresh |
| Editorial Rewrite (`editorial_rewrite`) | Rework existing copy for tone and clarity |
| `greenfield-project-v1` | Greenfield project scaffolding |
| `sprint-runner-v1` | Multi-issue sprint runner |

Each use case is a template. Browse them with `orch templates list` or search with `orch templates search <topic>`.

---

## Features

- ✅ **YAML-first pipeline definitions** — version-controlled, diff-friendly, no code required
- ✅ **Phase sequencing with dependency graphs** — topological sort, parallel wave execution
- ✅ **Model tier selection per phase** — haiku / sonnet / opus, set per phase or pipeline-wide
- ✅ **`skill_refs` injection** — pass tool contexts into prompts declaratively
- ✅ **Multiple execution backends** — Anthropic (standalone), OpenRouter (any model, primary path), plus experimental Gemini-CLI and Claude-Code executors. See [maturity tiers](docs/CURRENT-STATE.md#executor-maturity) for what's production-ready vs experimental.
- ✅ **Template index & search** — community index, install by GitHub shorthand `user/repo`
- ✅ **Scenario-based grading** — YAML acceptance criteria, LLM judges, assertion graders
- ✅ **Human-in-the-loop** — pause phases for review, inject feedback, resume
- ✅ **OpenClaw integration** *(deprecated — gateway no longer active; kept for historical runs)* — run phases as sub-agents with full tool access
- ✅ **Local web UI** — browse templates, start runs, and watch live progress in your browser (`orch serve`)

---

## How It Compares

| Feature | Orchemist | LangGraph | CrewAI | Autogen | Dify |
|---------|:-------------------:|:---------:|:------:|:-------:|:----:|
| YAML-first | ✅ | ❌ | ❌ | ❌ | Partial |
| Visual builder | 🔜 | ⚠️ | ❌ | ❌ | ✅ |
| Template library | ✅ | ❌ | ❌ | ❌ | ✅ |
| Testing / grading | ✅ | ❌ | ⚠️ | ❌ | ❌ |

> ✅ full support · ⚠️ partial/unofficial · ❌ not supported · 🔜 planned
>
> Comparison reflects our reading of each tool's defaults as of 2026-06; frameworks evolve — verify against their current docs.

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
        PS --> EX["Executors\n(Anthropic · OpenRouter\nGemini · ClaudeCode · Dry-Run)"]
    end

    Runner --> SR

    subgraph SR["Scenario Runner (optional acceptance testing)"]
        AG["Assertion Grader"] ~~~ LJ["LLM Judge"] ~~~ UC["URL Check"]
    end
```

Execution modes are listed in [Execution modes](#execution-modes) above; standalone (`ANTHROPIC_API_KEY`) and dry-run (no credentials) are the simplest places to start.

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

# Run the bundled hello-pipeline in dry-run — a working pipeline in 30 seconds, zero config
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

Three install paths, ordered by setup cost. Most readers want **Path A** if they already use Claude Code.

### Path A — Claude Code Skills Pack (no Python, ~1 minute)

If you already have [Claude Code](https://claude.com/claude-code), the simplest way to try the Orchemist coding pipeline is the Skills Pack — pure markdown, no engine, no server, BYO model:

```bash
git clone https://github.com/ToscanAI/orchemist-skills.git
cd orchemist-skills
./install.sh                 # drops skills into ~/.claude/skills/ and ~/.claude/agents/
claude                       # then: /orchemist:run examples/example-issue.md
```

Trades off: no web UI, no queue, no daemon, no multi-provider model selection. Use the engine (Path B / C) for those.

### Path B — From PyPI (engine + CLI, ~5 minutes)

```bash
pip install orchemist
orch --help
orch serve                   # local web UI at http://127.0.0.1:8374
```

### Path C — From Source (development, ~10 minutes)

```bash
git clone https://github.com/ToscanAI/orchemist.git
cd orchemist
python3 -m venv .venv && source .venv/bin/activate
pip install .
orch --help
```

---

## Relationship to OpenClaw

| Layer | Provides | Think of it as… |
|-------|----------|-----------------|
| **OpenClaw** | Sub-agent spawning, tool access, model switching | Operating System |
| **Orchemist** | Pipeline templates, phase sequencing, quality gates | Application Framework |
| **Scenario Runner** | Outcome-based testing, LLM judges, grading | Test Framework |

The engine runs **standalone** (direct Anthropic API) or via **OpenRouter** for multi-provider model choice; the deprecated OpenClaw gateway path also remains. Provider coverage is tiered — see [maturity tiers](docs/CURRENT-STATE.md#executor-maturity) — and broadening it (Ollama, more providers) is tracked in [#101](https://github.com/ToscanAI/orchemist/issues/101).

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

## Current Status

Orchemist is in **active development (alpha)**. What works, what doesn't:

| Area | Status |
|---|---|
| Coding pipeline (openrouter mode) | Primary path — first successful end-to-end run 2026-04-17, 32/32 acceptance tests passing |
| Coding pipeline (openclaw mode) | Deprecated — used internally before the gateway was sunset; kept for historical reference |
| Pipeline templates | `coding-pipeline-standard` (12 phases) and `coding-pipeline-skip-spec` (9 phases) stable; content, research, and docs templates available |
| Web UI | Functional for template browsing, run monitoring, and pipeline launching |
| Orchemist IDE | Sunset in favor of the harness (#814); see Harness above |
| OpenRouter tool calling | Shipped 2026-04-16 (PR #795) — 6 tools with path sandbox, retry, JSONL logging |
| Cost optimization | In progress — command/acceptance_run phases being routed away from LLM (#798) |
| Documentation | Partial — OpenRouter setup guide, getting started, template authoring available; end-to-end tutorial pending |
| Community templates | Not yet — contributions welcome |

Known limitations:
- Spec adversary occasionally doesn't comply with first-line verdict format (pipeline compensates via retry loops at cost of extra rounds)
- Cost reporting fallback overestimates by ~3x when OpenRouter doesn't return `usage.total_cost` (#801)
- Bash tool sandbox is a UX guardrail, not a security boundary — production deployments should use OS-level isolation (firejail, containers)

---

## Contributing

Pull requests are welcome! Here's how to get started:

```bash
git clone https://github.com/ToscanAI/orchemist.git
cd orchemist
pip install -e ".[dev]"
pytest
```

**Maintainers cutting a release:** see [docs/RELEASE-SOP.md](docs/RELEASE-SOP.md).

**Areas where contributions are especially welcome:**

- 📦 **Community templates** — add a YAML template to `templates/` and submit a PR
- 🧪 **Scenarios & rubrics** — improve grading quality for existing templates
- 🔌 **Executors** — add support for new model providers (Gemini, Mistral, local models)
- 📖 **Documentation** — improve examples, add tutorials, translate docs

**Filing issues:** Use the right template for the right pipeline:

| Issue type | Template | Pipeline |
|------------|----------|----------|
| Bug | Bug Report | `coding-pipeline-standard` |
| Feature / code change | Feature Request | `coding-pipeline-standard` |
| Documentation | Documentation Request | `docs-pipeline-v1` |
| Article / blog post | Content Request | `content-pipeline` |
| Research / analysis | Research Request | `research-competitive` |

Please read CONTRIBUTING.md for code style and PR guidelines.

---

## Documentation

- [Getting Started](docs/GETTING_STARTED.md) — detailed setup guide
- [Architecture](docs/ARCHITECTURE.md) — system design
- [Current State & Limitations](docs/CURRENT-STATE.md) — what works today, executor maturity tiers, known limitations, and what's planned
- [API Reference](docs/api-reference.md) — CLI commands + Python classes
- [Web UI](docs/web-ui.md) — browser interface (`orch serve`)
- [Harness redesign pack](docs/harness-redesign-2026-05-24/) — vision, duplicate audit, frontend audit, autonomy posture, SVG canon, live screenshots
- [Tech Stack](docs/tech-stack.md) — dependencies and choices
- [Security Policy](SECURITY.md) — vulnerability reporting & supported versions

---

## License

MIT © [Conny Lazo](https://connylazo.com) & [Toscan](https://github.com/ToscanAI)

See [LICENSE](LICENSE) for the full text.

---

**Orchemist — Tests passing. 4 execution modes. Git-native pipeline handoff.**
