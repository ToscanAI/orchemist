# Architecture — How It Works

> **Audience:** You've heard of AI agents. Maybe you've played with ChatGPT. You haven't necessarily built a pipeline system before. That's fine. This document explains how this engine works in plain language.

---

## What Is This?

This is an **orchestration engine** — a system that breaks a big task (say, "write me a well-researched LinkedIn article") into smaller steps, hands each step to an AI model, checks the quality of the result, and passes it along to the next step. Think of it like a small factory: raw material goes in one end, finished product comes out the other, with different workers handling different stations.

The key idea is that no single AI call does everything. Instead, one model researches, another writes, a third fact-checks, and a fourth polishes. Each model is chosen for the job: cheap and fast when that's enough, more powerful when the work is harder. The whole thing is driven by a plain YAML file — so you can define a new workflow without touching Python code.

---

## The Big Picture

Here's the full flow from your YAML file to a finished result:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        You write a YAML file                        │
│                    (defines phases, prompts, models)                │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
               ┌──────────┴──────────┐
               │  CLI: orch run      │  Web UI: orch serve
               │  (terminal)         │  (browser)
               └──────────┬──────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Template Engine                               │
│   • Reads and validates your YAML                                   │
│   • Builds a dependency graph (which phases depend on which)        │
│   • Computes execution order using topological sort                 │
└─────────────────────────┬───────────────────────────────────────────┘
                          │  execution plan (ordered phases)
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Phase Sequencer                               │
│   • Walks through phases in order (or in parallel within a wave)   │
│   • Fills in the prompt for each phase (inserting previous outputs) │
│   • Submits each phase as a Task to the runner                      │
│   • Retries failed phases (phase.retries + retry_delay_seconds)    │
│   • Stops the pipeline if any phase permanently fails               │
└─────────────────────────┬───────────────────────────────────────────┘
                          │  TaskSpec (prompt + model + timeout)
                          │
               ┌──────────┴──────────┐
               │  Progress Heartbeat  │  (non-TTY: emits status every
               │  (heartbeat.py)      │   30s to stdout for CI/cron)
               └──────────┬──────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Task Runner                                  │
│   • Picks the right Executor for the task type                      │
│   • Manages retries and model escalation on failure                 │
│   • Logs everything to SQLite                                       │
└──┬──────────┬───────────────────────┬────────────────────┬──────────┘
   │          │                       │                    │
   ▼          ▼                       ▼                    ▼
┌──────────┐ ┌────────────────┐ ┌──────────────────┐ ┌──────────────┐
│ DryRun   │ │ Anthropic      │ │ OpenClaw         │ │ OpenAI-      │
│ Executor │ │ Executor       │ │ Executor         │ │ Compatible   │
│ (testing)│ │ (direct API,   │ │ (sub-agents via  │ │ Executor     │
│          │ │  stdlib only)  │ │  CLI / file IPC) │ │ (Gemini,     │
└──────────┘ └────────────────┘ └────────────────┬─┘ │  Ollama, …)  │
                                                  │   └──────────────┘
                                                  │         ▲
                                                  │   (fallback via
                                                  │    FallbackHandler
                                                  │    on rate_limit/
                                                  │    timeout)
                                                  ▼
                                      ┌───────────────────────────────┐
                                      │          Sub-Agent            │
                                      │   (Claude Haiku / Sonnet /    │
                                      │    Opus — depends on phase)   │
                                      └───────────────┬───────────────┘
                                                      │  result JSON
                                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     Results & Phase Outputs                         │
│   • Each phase result is stored and passed to the next phase        │
│   • Final output is the result of the last phase                    │
└─────────────────────────┬───────────────────────────────────────────┘
                          │  (optionally)
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Scenario Runner / Graders                     │
│   • Loads acceptance criteria from a YAML scenario file             │
│   • Assertion Grader: checks facts programmatically                 │
│   • LLM Judge: sends the output to a model with a rubric            │
│   • Computes weighted score, decides pass/fail                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Key Concepts

### Templates — The Playbook

A **template** is a YAML file that describes your entire pipeline. It's a recipe. It says: "first do this, then do that, and here's the prompt to use at each step."

Each template has:
- A unique **id** and **name** so you can refer to it
- A list of **phases** (the steps)
- Optionally, a **config schema** that documents what inputs the pipeline expects

Think of it like a stage directions script. The template doesn't execute anything — it just describes what should happen and in what order.

```yaml
id: my-pipeline
name: "My First Pipeline"
phases:
  - id: research
    name: "Research Phase"
    ...
  - id: write
    name: "Writing Phase"
    depends_on: [research]
    ...
```

The `depends_on` field is the critical piece: it tells the engine "don't start `write` until `research` is finished." The engine automatically figures out the right order using a standard algorithm (topological sort). If you have two phases that don't depend on each other, they're identified as independent — though the current version runs them one at a time.

---

### Phases — The Steps

A **phase** is one unit of work inside a pipeline. Each phase:
- Has a **task type** (`research`, `content`, `code`, `review`, `translation`)
- Specifies a **model tier** — how capable the AI needs to be
- Has a **prompt template** — the instructions sent to the AI
- Has a **timeout** — how long to wait before giving up

The prompt template is a Python format string. You can embed variables like `{input[brief]}` (from your pipeline's initial input) or `{previous_output[research]}` (the output from an earlier phase). This is how data flows: phase outputs become inputs to the next phase's prompt.

If a key is missing — say, you reference `{previous_output[some_phase]}` but that phase hasn't run — you get a `<MISSING:some_phase>` placeholder instead of a crash. The engine is designed to keep running and let you debug rather than dying silently.

---

### Executors — The Workers

An **executor** is the thing that actually *does* the work. There are five built in:

| Executor | What it does | When it's used |
|---|---|---|
| **AnthropicExecutor** | Calls the Anthropic Messages API directly via `urllib` | `standalone` mode; no framework dependency |
| **OpenClawExecutor** | Calls an AI sub-agent via the OpenClaw CLI or file-based IPC | `openclaw` mode; full tool access via sub-agents |
| **OpenAICompatibleExecutor** | Calls any OpenAI-compatible `/v1/chat/completions` endpoint | Gemini-via-proxy, Ollama, LM Studio, or other providers |
| **DryRunExecutor** | Returns mock results instantly | Testing, development, and CI pipelines |
| **FallbackHandler** | Wraps a primary executor; retries via `OpenAICompatibleExecutor` on retriable errors | Automatic fallback when Anthropic rate-limits or times out |

The `AnthropicExecutor` is the primary executor for standalone use. It:
1. Resolves the model tier (`haiku` / `sonnet` / `opus`) to the exact Anthropic model string
2. Optionally enables extended thinking (2 K–32 K tokens budget)
3. Calls the Anthropic Messages API using only stdlib `urllib` — no third-party HTTP library required

The `OpenClawExecutor` is the integration executor. It:
1. Formats the phase prompt into a clean task
2. Tries to run `openclaw agent run` — a CLI that spawns a sub-agent
3. If the CLI isn't installed, it falls back to writing an `input.json` file to `~/.orchestration-engine/tasks/<id>/` and polling for an `output.json` file that another process will produce

This file-based handoff is a simple but robust integration contract: the engine and OpenClaw communicate through the filesystem, which means they can run in different processes, even on different schedules.

**Model escalation on failure:** If a task fails, the engine automatically retries with a more capable model. A content task might start on Haiku, fail, retry on Sonnet, fail again, and finally retry on Opus. This means you get cheap results when things go well and more thorough results when they don't — without manual intervention.

**Phase-level retry logic:** Independently from the executor-level retry, each phase in a template can declare its own `retries` count and `retry_delay_seconds` delay. When a phase fails, the sequencer retries it up to `retries` additional times (default 0 = no retries), waiting `retry_delay_seconds` between attempts. This is coarser-grained than the executor's internal retry but useful for transient network errors or model overload.

---

### Graders — The Quality Checkers

Once a pipeline runs, how do you know the output is actually good? That's what **graders** are for. They're used by the Scenario Runner (see below), and there are three kinds:

**Assertion Grader** — Checks a Python expression against the output. For example:
```yaml
check: "len(output.get('article', '')) > 500"
```
This verifies the article is longer than 500 characters. The check runs inside a strict sandbox: only a small set of safe operations (comparisons, `len`, `str`, `int`, `bool`) are allowed. The expression is parsed into an AST (abstract syntax tree) first, and any dangerous operations (imports, class introspection, dunder methods) are blocked before the code ever runs.

**LLM Judge Grader** — Sends the output to a judge model along with a rubric. The judge reads the article and scores it 0.0–1.0. The rubric can be written in plain English: "Does this article cite its sources? Is the tone appropriate for LinkedIn?" The judge never sees the scenario's pass threshold or any pipeline metadata — only the article and the rubric. This "holdout principle" prevents the judge from gaming its own score.

**URL Check Grader** — Verifies that URLs mentioned in the article are real and reachable.

Each grader returns a score and a pass/fail decision. Some criteria can be **gates**: if a gate fails (e.g., the article is empty), the entire scenario fails immediately, regardless of how well the other criteria scored.

---

### Scenarios — The Acceptance Tests

A **scenario** is like a test case for your pipeline. It defines:
- What output to expect
- A list of acceptance criteria (using the graders above)
- A pass threshold (e.g., 75% weighted score required to pass)

You run scenarios after a pipeline to verify it's working correctly. This is the equivalent of unit tests, but for AI output.

```yaml
id: content-pipeline-happy-path
acceptance:
  - id: article-exists
    type: assertion
    check: "'article' in output"
    weight: 0   # gate: must pass or everything fails

  - id: factual-quality
    type: llm_judge
    rubric: "Rate factual accuracy from 0.0 to 1.0. Output: Score: X.X"
    weight: 3
    threshold: 0.7
```

A **suite** is a folder of scenarios. You can run all of them at once and get a satisfaction rate (e.g., "8 out of 10 scenarios passed").

---

## How a Pipeline Runs — Step by Step

Let's walk through the **content pipeline** template (`content-pipeline.yaml`) to make this concrete. Suppose you ask it to write a LinkedIn article about remote work trends.

### Phase 1: `research` (Sonnet)
The sequencer takes your input (`brief: "Remote work trends in 2025"`, `target_audience: "HR leaders"`) and fills it into the research prompt. A Sonnet sub-agent is spawned and told: "Research this topic. Find 10+ credible sources. Output a structured brief with citations, key facts, and statistics."

The result — a JSON blob with sources, facts, and data points — is stored in `phase_outputs["research"]`.

### Phase 2: `write` (Sonnet)
This phase depends on `research`, so it runs next. The sequencer fills in the write prompt with `{previous_output[research]}` — the full research brief from Phase 1. A Sonnet sub-agent writes the article draft: headline, intro, three sections, conclusion, sources.

The draft is stored in `phase_outputs["write"]`.

### Phase 3: `fact_check` (Sonnet)
This phase depends on *both* `write` and `research`. The prompt includes the draft article AND the research sources, and asks: "For each factual claim in the article, is it supported by the research? List issues. Give an accuracy score 0–100."

The result — a list of issues plus an accuracy score — is stored in `phase_outputs["fact_check"]`.

### Phase 4: `apply_fixes` (Sonnet)
Depends on `write` and `fact_check`. The prompt includes both the original draft and the list of fact-check issues. The sub-agent rewrites the article, fixing only the flagged inaccuracies while preserving the structure and tone.

### Phase 5: `final_output` (Sonnet)
Depends only on `apply_fixes`. The corrected article is polished for publication: proper headline, clean section headings, formatted Sources section. This is the final deliverable.

The entire pipeline returns a `phase_outputs` dict (every phase's result) plus a `final_output` pointing to Phase 5's result.

---

## The Database

The engine uses **SQLite** stored at `~/.orchestration-engine/engine.db`. SQLite is a file-based database — there's nothing to install or configure. It just works.

Four tables are maintained:

| Table | What's stored |
|---|---|
| `tasks` | Every task submitted: type, status, payload, retry count, model, cost |
| `task_runs` | Every execution attempt, including the model used, tokens consumed, cost, and error messages |
| `orchestras` | Multi-phase pipeline runs: which template, progress, budget |
| `dead_letter_queue` | Tasks that permanently failed after all retries, for analysis |

Why store all this? A few reasons:
- **Retries**: If a task fails, the engine needs to know the retry count and when to try again
- **Auditing**: You can look back and see exactly what happened, what model was used, what it cost
- **Dead letter analysis**: Tasks that never succeed are preserved for inspection rather than silently dropped
- **Resume**: If the process crashes mid-pipeline, state is not lost

The database uses WAL (Write-Ahead Logging) mode, which means reads and writes don't block each other — important when multiple workers are running concurrently.

---

## Model Tiers

Three tiers are available. The right choice depends on what the task needs:

| Tier | Model | Speed | Cost | Best For |
|---|---|---|---|---|
| **Haiku** | `claude-haiku-4-5` | Fastest | Cheapest | Simple lookups, classification, translation, first attempts |
| **Sonnet** | `claude-sonnet-4` | Fast | Moderate | Writing, research, most production work |
| **Opus** | `claude-opus-4-6` | Slowest | Most expensive | Complex reasoning, final-resort retries, creative judgment |

In your YAML template, you set the tier per phase:
```yaml
model_tier: haiku    # or sonnet, or opus
thinking_level: low  # or medium or high
```

`thinking_level` controls how much "internal reasoning" the model does before answering. `low` is fast. `high` is thorough but slower and more expensive.

**Automatic escalation:** If a task fails and is retried, the engine escalates to the next tier automatically. A content task starts on Haiku, retries on Sonnet, final attempt on Opus. Code tasks start on Sonnet (they need more capability from the start). You don't have to configure this — it's built into the escalation paths.

---

## Security

This engine evaluates code (in the Assertion Grader) and spawns sub-processes. Here's what's done to keep that safe:

### AST-Validated Eval (Assertion Grader)
User-supplied expressions like `len(output['article']) > 500` are evaluated — but not blindly. The expression is first parsed into an abstract syntax tree (AST), and every node is checked against an allowlist. Allowed: comparisons, boolean ops, arithmetic, subscript access, `len`, `str`, `int`, `float`, `bool`. Blocked: imports, class access, dunder methods (`__import__`, `__class__`, `__builtins__`, etc.), file operations, anything not on the list. If a blocked construct is found, the expression is rejected before it ever runs.

### Path Traversal Protection (OpenClawExecutor)
When the executor writes task files to `~/.orchestration-engine/tasks/<task_id>/`, the task ID is validated against a strict regex (`^[a-zA-Z0-9_-]+$`). If the ID contains path components like `../../../etc/passwd`, it's replaced with a fresh UUID. After creating the directory, its resolved path is verified to be inside the tasks root — a defense-in-depth check that catches edge cases even if the ID validation passed.

### Input Sanitization (LocalExecutor)
Shell commands from task payloads are parsed with `shlex.split()` and executed with `shell=False`. This means no shell interpretation: backticks, pipes, redirects, and semicolons are treated as literal characters, not shell operators. Additionally, only an allowlist of executables (`echo`, `ls`, `cat`, `python`, etc.) can be invoked.

### Rubric File Path Validation (Scenario Runner)
When a scenario references an external rubric file, the resolved path is checked to be within the scenarios directory. If the path escapes the directory (e.g., via `../../secret`), an error is raised rather than reading an arbitrary file.
