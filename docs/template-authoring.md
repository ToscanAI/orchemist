# Template Authoring Guide

> **The definitive reference for writing pipeline templates for the Orchestration Engine.**
> Covers every field, every variable, every pattern, and every pitfall.

---

## Table of Contents

1. [Quick Reference](#1-quick-reference)
2. [Full Field Reference](#2-full-field-reference)
- [Composition (`extends:` / `exclude_phases:`)](#composition)
3. [Annotated Example](#3-annotated-example)
4. [config_schema Reference](#4-config_schema-reference)
5. [Variable Interpolation Guide](#5-variable-interpolation-guide)
6. [Cookbook Patterns](#6-cookbook-patterns)
7. [Testing Your Template](#7-testing-your-template)
8. [Troubleshooting](#8-troubleshooting)
- [Pitfalls & Pre-PR Checklist](#pitfalls--pre-pr-checklist)

---

## 1. Quick Reference

A valid template is a YAML file with two required fields (`id`, `name`) and a list of phases. Every other field is optional but recommended for production use.

```yaml
# ── Template Header ───────────────────────────────────────────────────────────
id: my-pipeline                          # required · unique slug
name: "My Pipeline"                      # required · display name
version: "1.0.0"                         # semver · default "1.0.0"
description: "What this pipeline does."  # shown in orch templates list
author: "Your Name"                      # recommended for completeness
category: "content"                      # content | code | research | etc.
tags: [content, writing]                 # for search
use_cases:                               # shown in orch templates info
  - "Generate blog posts"
example_input:                           # used by orch start wizard
  topic: "AI safety"

# ── Input Schema ──────────────────────────────────────────────────────────────
config_schema:
  type: object
  properties:
    topic:
      type: string
      description: "Article topic"
  required: [topic]

# ── Phases ────────────────────────────────────────────────────────────────────
phases:
  - id: research                         # required · unique within template
    name: "Research Phase"               # required · display name
    description: "Gather sources."       # optional
    task_type: research                  # content | research | review | code | translation
    model_tier: sonnet                   # haiku | sonnet | opus
    thinking_level: low                  # off | low | medium | high
    depends_on: []                       # phase IDs that must complete first
    timeout_minutes: 30                  # default 30
    human_review: false                  # pause for human approval if true
    skill_refs: []                       # paths to external skill context files
    prompt_template: |
      Research: {input[topic]}
    output_schema:                       # optional · documents expected output shape
      sources:
        type: array

  - id: write
    name: "Write Draft"
    task_type: content
    model_tier: sonnet
    thinking_level: medium
    depends_on: [research]
    prompt_template: |
      Based on: {previous_output[research]}
      Write an article about {input[topic]}.
```

**Execution order:** Phases with no `depends_on` run in Wave 1. Each subsequent wave runs after all its dependencies are satisfied. Cycles are rejected at validation time.

---

## 2. Full Field Reference

### 2.1 Template-Level Fields

| Field | Type | Required | Default | Description |
|---|---|:---:|---|---|
| `id` | string | ✅ | — | Unique identifier. Used as the template name in CLI commands. Use `kebab-case`. Example: `content-pipeline-v2` |
| `name` | string | ✅ | — | Human-readable display name. Shown in `orch templates list`. Example: `"Content Pipeline v2"` |
| `version` | string | — | `"1.0.0"` | Semantic version (`X.Y.Z`). Required by extended linting (`orch validate`). |
| `description` | string | — | `""` | One-paragraph description. Supports YAML block scalar (`>`). Required by extended linting. |
| `author` | string | — | `""` | Template author. Required by extended linting. |
| `category` | string | — | `""` | Grouping category. Common values: `content`, `code`, `research`, `translation`. |
| `tags` | list[string] | — | `[]` | Searchable labels. Used by `orch templates search`. |
| `use_cases` | list[string] | — | `[]` | Short bullet list of intended use cases. Shown by `orch templates info`. Recommended by extended linting. |
| `example_input` | dict | — | `{}` | Example input values. Used by `orch start` wizard to pre-fill prompts. Recommended by extended linting. |
| `phases` | list[Phase] | — | `[]` | Ordered list of phase definitions. A template with no phases is technically valid but useless. |
| `config_schema` | dict | — | `{}` | JSON Schema describing the template's accepted inputs. Drives `orch start` form rendering and `orch validate` checks. |
| `fallback` | dict | — | `null` | Fallback executor configuration. Optional; used for Gemini fallback when Anthropic is unavailable. |
| `parallel` | bool | — | `true` | Enable concurrent execution of independent phases within the same wave. Set `false` for purely sequential execution. |
| `max_parallel` | int | — | `0` | Max concurrent phases per wave. `0` = unlimited (pool matches wave size). Positive values cap the thread pool. |
| `fail_fast` | bool | — | `true` | Abort remaining phases in a parallel wave when one fails. Set `false` to collect all errors. |
| `default_transitions` | dict | — | `{}` | Pipeline-wide outcome→phase_id map. Applied to every phase that doesn't override a given outcome key. See [State Machine Transitions](#69-state-machine-transitions). |
| `max_iterations` | int | — | `10` | Max iterations for the state-machine loop when transitions are used. Prevents infinite cycles. |
| `scenario` | string | — | `null` | Path to a scenario YAML for post-pipeline auto-scoring. Required for `category: code` templates. |
| `auto_merge` | dict | — | `null` | Auto-merge configuration for git-enabled pipelines. See [Auto-Merge Config](#211-auto-merge-config). |
| `on_complete` | dict | — | `null` | Pipeline chaining configuration. See [Pipeline Chaining Config](#212-pipeline-chaining-config). |
| `routing_config` | dict | — | `null` | Confidence-based routing tiers. See [Routing Config](#213-routing-config). |
| `budget` | dict | — | `null` | Cost limits and alerting. See [Budget Config](#214-budget-config). |
| `git` | dict | — | `null` | Git lifecycle management (branch, commit, push, merge gate). See [Git Config](#215-git-config). |

#### 2.1.1 Auto-Merge Config

Controls automatic PR merging after scoring completes. Requires a git-enabled pipeline.

```yaml
auto_merge:
  enabled: true                 # default: false
  min_score: 0.90               # minimum composite score to auto-merge
  require_approve: true         # require gate approval even if score passes
  strategy: squash              # squash | merge | rebase
  review_phase_id: review       # phase ID whose output is used as PR description
```

| Sub-field | Type | Default | Description |
|---|---|:---:|---|
| `enabled` | bool | `false` | Enable auto-merge behavior |
| `min_score` | float | `0.90` | Minimum composite score threshold |
| `require_approve` | bool | `true` | Require `orch gate approve` even with passing score |
| `strategy` | string | `"squash"` | Git merge strategy: `squash`, `merge`, or `rebase` |
| `review_phase_id` | string | `"review"` | Phase whose output populates the PR description |

#### 2.1.2 Pipeline Chaining Config

Triggers child pipelines on completion. Enables `success → deploy` or `failed → notify` flows.

```yaml
on_complete:
  success:
    - template: deploy-staging
      input_map:
        artifact_path: "{{output_dir}}/build"
    - template: notify-slack
      input_map:
        message: "Pipeline succeeded"
  failed:
    - template: alert-team
      input_map:
        error: "{{final_output.errors}}"
  max_chain_depth: 5             # default: 5; hard limit: 20
```

| Sub-field | Type | Default | Description |
|---|---|:---:|---|
| `success` | list | `[]` | Templates to launch on successful completion |
| `failed` | list | `[]` | Templates to launch on failure |
| `max_chain_depth` | int | `5` | Maximum chaining depth to prevent infinite recursion |

Each entry has `template` (name or path) and `input_map` (dict with `{{placeholder}}` interpolation from the parent run's context).

#### 2.1.3 Routing Config

Confidence-based routing of pipeline results to different actions.

```yaml
routing_config:
  tiers:
    - name: auto_merge
      min_score: 0.95
      requires: [tests_pass, no_security_findings]
    - name: human_review
      min_score: 0.70
      max_score: 0.95
      notify: [slack, github_pr_comment]
    - name: auto_retry
      min_score: 0.40
      max_score: 0.70
      strategy: escalate_model
      max_retries: 2
    - name: reject
      max_score: 0.40
```

| Tier sub-field | Type | Default | Description |
|---|---|:---:|---|
| `name` | string | required | Tier identifier |
| `min_score` | float | `0.0` | Minimum score for this tier (inclusive) |
| `max_score` | float | `1.0` | Maximum score for this tier (exclusive) |
| `requires` | list | `[]` | Additional conditions that must be met |
| `notify` | list | `[]` | Notification channels to alert |
| `strategy` | string | `null` | Retry strategy (for retry tiers): `escalate_model`, `split_task`, `add_context` |
| `max_retries` | int | `0` | Max retries for this tier |

When absent, the engine uses a built-in default routing configuration.

#### 2.1.4 Budget Config

Enforces per-run and per-day cost limits. The daemon aborts a run if the per-run cap is exceeded; launches are rejected when the daily cap is reached.

```yaml
budget:
  max_cost_per_run: 5.00        # USD; null = unlimited
  max_cost_per_day: 50.00       # USD; null = unlimited
  warn_at_percentage: 80.0      # alert at 80% of budget
```

| Sub-field | Type | Default | Description |
|---|---|:---:|---|
| `max_cost_per_run` | float | `null` | Maximum USD spend per pipeline run |
| `max_cost_per_day` | float | `null` | Maximum USD spend per calendar day |
| `warn_at_percentage` | float | `80.0` | Percentage of budget at which to emit a warning |

#### 2.1.5 Git Config

Controls the full git lifecycle: branching, committing, pushing, and merge gating.

```yaml
git:
  enabled: true                  # default: false
  branch_pattern: "feat/{pipeline_id}-{run_id}"
  auto_commit: true
  commit_phases: [implement, fix]  # only commit after these phases (empty = all)
  working_dir: "."
  push: true
  merge_gate: true               # create a gate requiring orch gate approve
  create_pr: false               # auto-create a GitHub PR on gate approval
  base_branch: main              # null = auto-detect default branch
```

| Sub-field | Type | Default | Description |
|---|---|:---:|---|
| `enabled` | bool | `false` | Enable git lifecycle for this pipeline |
| `branch_pattern` | string | `"feat/{pipeline_id}-{run_id}"` | Branch name pattern with `{placeholder}` interpolation |
| `auto_commit` | bool | `true` | Automatically commit after each phase |
| `commit_phases` | list | `[]` | Only commit after these phase IDs (empty = all phases) |
| `working_dir` | string | `"."` | Working directory for git operations |
| `push` | bool | `true` | Push commits to remote |
| `merge_gate` | bool | `true` | Create a merge gate requiring human approval |
| `create_pr` | bool | `false` | Auto-create a GitHub PR on gate approval |
| `base_branch` | string | `null` | Base branch to merge into (`null` = auto-detect) |

### 2.2 Phase-Level Fields

| Field | Type | Required | Default | Description |
|---|---|:---:|---|---|
| `id` | string | ✅ | — | Unique phase identifier within the template. Used in `depends_on` references and variable interpolation. Use `snake_case` or `kebab-case`. |
| `name` | string | ✅ | — | Human-readable phase display name. Shown in `orch list-phases` output. |
| `description` | string | — | `""` | What this phase does. Displayed in `orch templates info` and pipeline run logs. |
| `task_type` | string | — | `"content"` | Determines the executor's model escalation path and system prompt. Values: `content`, `research`, `review`, `code`, `translation`, `analysis`, `compliance`, `financial`, `support`, `triage`, `sales`. |
| `model_tier` | string | — | `"sonnet"` | Starting model tier for this phase. Values: `haiku`, `sonnet`, `opus`. On retry, the engine escalates automatically (see escalation table in API reference). |
| `thinking_level` | string | — | `"low"` | Extended thinking token budget. Values: `off` (0 tokens), `low` (2,048), `medium` (8,192), `high` (32,768). Higher = more expensive but better reasoning. |
| `depends_on` | list[string] | — | `[]` | Phase IDs that must succeed before this phase runs. Empty list (or omitting the field) means the phase runs in Wave 1. |
| `timeout_minutes` | int | — | `30` | Maximum wall-clock time for this phase. Exceeded tasks are failed and retried. |
| `human_review` | bool | — | `false` | If `true`, the pipeline pauses after this phase and waits for a human to approve before continuing. Useful as a quality gate before publication or deployment. |
| `prompt_template` | string | — | `""` | The prompt sent to the model. Supports Python `str.format()`-style variable interpolation (see Section 5). Use YAML block scalar (`|`) for multi-line prompts. |
| `output_schema` | dict | — | `{}` | Describes the expected output shape (informal documentation). Not enforced at runtime but used by tooling and human readers. |
| `skill_refs` | list[string] | — | `[]` | Paths to skill context files. Resolved relative to the template directory first, then `~/.orch/skills/`. Contents are injected into the prompt context as `{skill_context[name]}`. Validated by `orch validate`. |
| `retries` | int | — | `0` | Number of **additional** attempts if the phase fails. `0` means the phase is tried once and not retried on failure. `2` means up to 3 total attempts. |
| `retry_delay_seconds` | float | — | `30.0` | Seconds to wait between retry attempts for this phase. Only meaningful when `retries > 0`. |
| `write_files` | bool | — | `false` | When `true`, the sequencer parses `FILE` blocks from the phase output and writes them to `working_dir`. Used for code-generation phases. |
| `working_dir` | string | — | `"."` | Directory where extracted files are written (when `write_files` is enabled). |
| `base_dir` | string | — | `""` | Safety root directory — file writes are refused outside this boundary. Empty = use `working_dir` as boundary. |
| `context_files` | list[string] | — | `[]` | Paths to local files whose contents are inlined into the phase prompt as additional context. |
| `transitions` | dict | — | `{}` | Per-phase outcome→phase_id routing map. Merges on top of `default_transitions`. Used by the `StateMachineSequencer`. See [State Machine Transitions](#69-state-machine-transitions). |
| `max_iterations` | int | — | `0` | Per-phase iteration limit override. `0` = use the pipeline-level `max_iterations` default. |
| `command` | string | — | `null` | Shell command to execute. Used when `task_type: command`. The engine runs this command directly instead of calling an LLM. |
| `allowed_commands` | list[string] | — | `[]` | Security allowlist of command prefixes. Only commands starting with one of these strings are permitted. |
| `supervisor` | bool | — | `false` | Enable a supervisor evaluation hook after this phase. The supervisor LLM reviews the output and issues an APPROVE, REVISE, or ABORT verdict. |
| `supervisor_prompt` | string | — | `null` | Custom supervisor evaluation prompt. Supports `{rubric}` and `{phase_output}` placeholders. Uses a built-in default if `null`. |
| `supervisor_model` | string | — | `"opus"` | Model tier override for the supervisor call. |
| `supervisor_rubric` | string | — | `null` | Quality criteria / rubric text injected into the supervisor prompt's `{rubric}` placeholder. |
| `supervisor_max_retries` | int | — | `2` | Maximum REVISE→re-execute cycles before the supervisor aborts. |
| `model_chain` | list[string] | — | `[]` | Ordered list of model tiers to try on retry (e.g. `["sonnet", "opus"]`). Empty = use executor's default escalation chain. |
| `min_output_length` | int | — | `0` | Minimum character count for phase output. `0` = disabled. When >0, the sequencer fails the phase if output is shorter (catches truncated LLM responses). |
| `protected_outputs` | list[string] | — | `[]` | Filenames (relative to output dir) to checksum-protect via `file_guard.py`. SHA256 hashes are computed after this phase and verified before the next consuming phase. |
| `provider` | string | — | `null` | Provider that runs **this** phase: `anthropic` or `openrouter` (see [§2.4](#24-per-phase-provider-targeting)). Omit to use the run-level provider. Unknown values are a validation **error**. |

**Phase field aliases:** `prompt` → `prompt_template`, `model` → `model_tier`. Either form is accepted in YAML.

**Phase retry example:**

```yaml
phases:
  - id: research
    name: "Research"
    task_type: research
    model_tier: sonnet
    retries: 2                  # Retry up to 2 more times on failure (3 attempts total)
    retry_delay_seconds: 15.0   # Wait 15 seconds between attempts
    prompt_template: |
      Research: {input[topic]}
```

> **Note:** `retries` is a phase-level field — it controls how many times the *sequencer* retries a phase before giving up. It is independent of the executor-level `max_retries` on `TaskSpec`, which controls retries within a single execution attempt.

### 2.3 Model Tier Quick Guide

| Tier | Best For | Cost | Speed |
|---|---|---|---|
| `haiku` | Fast, simple tasks: classification, summarisation, keyword extraction | Cheapest | Fastest |
| `sonnet` | Most production phases: writing, reviewing, research, code generation | Moderate | Moderate |
| `opus` | Complex reasoning: architecture review, adversarial analysis, synthesis | Most expensive | Slowest |

**Auto-escalation on retry:** If a phase fails or doesn't meet `min_confidence`, the engine retries with a higher tier automatically (e.g. `haiku → sonnet → opus` for `content` tasks).

### 2.4 Per-Phase Provider Targeting

By default every phase in a run is executed by the run-level provider (chosen by `orch run --mode`). The `provider:` phase field lets a single pipeline **mix providers** — e.g. draft on a cheap `openrouter` model and review on a frontier Anthropic model — without splitting the run or losing the orchestration, retries, and cost ledger.

```yaml
phases:
  - id: draft
    name: "Draft"
    provider: openrouter        # this phase runs on OpenRouter
    model_tier: sonnet          # resolves through OpenRouter's model_map
    prompt_template: |
      Draft an outline for: {input[topic]}
  - id: review
    name: "Review"
    provider: anthropic         # this phase runs on Anthropic
    model_tier: opus
    depends_on: [draft]
    prompt_template: |
      Critically review the draft: {draft.output}
```

**Behaviour:**

- **Values:** `anthropic` or `openrouter` only. These are the two providers with real run-mode factories (`KNOWN_PROVIDERS`). `gemini`, `claudecode`, and `openclaw` are **not** per-phase providers in v1.1 (they have no per-phase credential story) — naming any of them in `provider:` is a validation **error**.
- **Omit → run-level default.** A phase with no `provider:` is selected exactly as today (the run's default executor), so existing templates are byte-identical. The run-level provider is always the no-`provider` fallback.
- **Auto-upgrade, no new flag.** When a template declares any `provider:`, `orch run` automatically builds one executor per referenced provider. Run it like any other template:

  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  export OPENROUTER_API_KEY=sk-or-...
  orch run mixed-pipeline.yaml --mode standalone
  ```

- **Both credentials must be present.** Each referenced provider's credential is resolved at build time; a missing one fails immediately, naming the provider and the env var (e.g. `OpenRouter API key required.`). In a mixed run, `--api-key` feeds **Anthropic only**; OpenRouter sources its key from `OPENROUTER_API_KEY` exclusively (one key cannot satisfy both providers).
- **`model_tier` resolves through the target provider.** A phase's `model_tier` is mapped by the *target* executor's own table — for `provider: openrouter` it goes through the OpenRouter `model_map` (see [OpenRouter `--model-map`](#openrouter-mode)); for `provider: anthropic` through the Anthropic registry. There is no new tier vocabulary.
- **Run-level OpenRouter config still applies.** `--base-url` / `--model-map` are run-level OpenRouter flags; in a mixed run they configure the single OpenRouter executor. Per-phase `base_url` / `model_map` are not supported in v1.1.
- **Dry-run works without credentials.** `orch run mixed-pipeline.yaml --mode dry-run` validates and runs a mixed-provider template with no real keys (every phase falls back to the dry-run executor).

---

## Composition

*(`extends:` / `exclude_phases:`)*

> This guide also covers template composition (this section) and common authoring pitfalls (the [Pitfalls & Pre-PR Checklist](#pitfalls--pre-pr-checklist) section near the end).

A template can build on another instead of repeating its phases. Set `extends: <other-template-id>` to inherit that template's phases plus its top-level `config_schema`, `auto_merge`, `routing_config`, and `example_input`. You then tailor the inherited pipeline by:

1. **Trimming** inherited phases with `exclude_phases: [...]`.
2. **Appending** new phases under `phases:`.
3. **Field-level overriding** an inherited phase by re-declaring it under `phases:` with the same `id` (only the fields you set change; the rest are inherited).

Inherited phases you neither exclude nor override are pulled in verbatim.

### Worked example: `coding-pipeline-skip-spec`

`coding-pipeline-skip-spec` is built entirely by composition (issue #704). Its header declares:

```yaml
extends: coding-pipeline-standard
exclude_phases:
  - existing_symbols_inventory
  - spec
  - behavioral
  - spec_adversary
  - postmortem_spec
```

What this does, exactly:

1. **Inherit** the shared phases and top-level config from `coding-pipeline-standard`.
2. **`exclude_phases:`** removes the five spec-loop phases listed above.
3. **Append** the skip-spec-only phases `acceptance_test_adversary` and `verify_tests_integrity` under `phases:`.
4. **Field-level override** the five shared phases that differ (`acceptance_test`, `implement`, `acceptance_run`, `review`, `fix`) by re-declaring them; `postmortem_review` and `test` are inherited verbatim (drift-locked).

Net result: the standard pipeline's 12 phases become skip-spec's 9.

### Verifying a composed template

After composition, confirm the resolved phase set with:

```bash
orch validate coding-pipeline-skip-spec
orch list-phases coding-pipeline-skip-spec
```

`orch list-phases` prints the fully-resolved order (inherited + appended − excluded), so you can check the composition produced exactly the phases you expect.

---

## 3. Annotated Example

The following is a complete, production-ready template with inline comments explaining every decision.

```yaml
# content-pipeline-annotated.yaml
# Run: orch run content-pipeline-annotated.yaml --input '{"topic":"AI in healthcare","tone":"professional","word_count":1500}'

# ── Identity ──────────────────────────────────────────────────────────────────
id: content-pipeline-annotated   # Unique slug. Matches the filename stem by convention.
name: "Annotated Content Pipeline"
version: "1.0.0"                  # Increment when you change phase logic or inputs.
description: >                    # Use > for folded block (newlines become spaces).
  A 5-phase content pipeline that researches a topic, drafts an article,
  runs parallel flow and adversarial review, applies fixes, and delivers
  a publication-ready piece. Annotated for learning purposes.
author: "Your Name"
category: "content"
tags:
  - content
  - article
  - review

# ── Use Cases & Example ───────────────────────────────────────────────────────
use_cases:
  - "Generate long-form blog posts with built-in quality review"
  - "Produce researched LinkedIn articles with adversarial quality checks"

example_input:                    # Values shown in `orch start` wizard; safe to commit.
  topic: "The future of remote work"
  tone: "professional"
  word_count: 1200

# ── Input Schema ──────────────────────────────────────────────────────────────
# Drives the orch start wizard and orch validate checks.
# See Section 4 for full config_schema reference.
config_schema:
  type: object
  properties:
    topic:
      type: string
      description: "Article topic or title"
    tone:
      type: string
      description: "Writing tone: professional, conversational, academic"
      default: "professional"
      enum: [professional, conversational, academic]
    word_count:
      type: integer
      description: "Target word count for the draft"
      default: 1500
      minimum: 500
      maximum: 5000
  required:
    - topic              # Only topic is mandatory; others have sensible defaults.

# ── Phases ────────────────────────────────────────────────────────────────────
phases:

  # Wave 1: No dependencies — runs immediately.
  # Use haiku/sonnet for lightweight gathering tasks.
  - id: research
    name: "Source Research"
    description: "Gather credible sources, key facts, and expert perspectives."
    task_type: research          # Escalation path: haiku → sonnet → opus on retry.
    model_tier: sonnet           # Sonnet is a good default for most phases.
    thinking_level: low          # Low thinking (2,048 tokens) — sufficient for research.
    depends_on: []               # Explicit empty list is equivalent to omitting depends_on.
    timeout_minutes: 30          # Generous timeout for web-heavy research tasks.
    prompt_template: |           # Use | (literal block) to preserve newlines.
      You are a research assistant. Gather comprehensive background on this topic.

      Topic: {input[topic]}
      Tone for final article: {input[tone]}

      Produce:
      1. A 2-paragraph topic summary
      2. At least 8 key facts or data points with source references
      3. Notable expert perspectives
      4. Current trends and recent developments
      5. Potential counterarguments

      Format as clearly labelled sections.

  # Wave 2: Depends on research — runs after research completes.
  - id: draft
    name: "Full Draft"
    description: "Write a complete article draft using the research brief."
    task_type: content
    model_tier: sonnet
    thinking_level: medium       # Medium thinking for higher-quality writing.
    depends_on:
      - research                 # This phase waits for research to succeed.
    timeout_minutes: 45          # Writing takes longer — give it extra time.
    prompt_template: |
      Write a complete article based on the research brief below.

      === RESEARCH BRIEF ===
      {previous_output[research]}    # References the output of the 'research' phase.

      === REQUIREMENTS ===
      Topic: {input[topic]}
      Tone: {input[tone]}
      Target word count: {input[word_count]}

      Produce only the article text (headline + body + conclusion).

  # Wave 3a: flow-review and red-team both depend on draft only,
  # so they are independent of each other and run in the same wave.
  - id: flow-review
    name: "Flow Review"
    description: "Check logical flow, transitions, and narrative coherence."
    task_type: review
    model_tier: sonnet
    thinking_level: low
    depends_on:
      - draft
    timeout_minutes: 20
    prompt_template: |
      Review this article for flow and coherence. Score it 1-10.

      === DRAFT ===
      {previous_output[draft]}

      Evaluate: logical flow, section transitions, opening hook, conclusion strength.
      For each issue: location, problem description, specific fix.

  # Wave 3b: Runs in parallel with flow-review (same wave).
  - id: red-team
    name: "Adversarial Review"
    description: "Challenge claims, find weaknesses, stress-test arguments."
    task_type: review
    model_tier: sonnet
    thinking_level: medium       # Higher thinking for adversarial reasoning.
    depends_on:
      - draft                    # Same single dependency as flow-review → same wave.
    timeout_minutes: 25
    prompt_template: |
      Perform an adversarial review. Challenge every claim. Find every weakness.

      === DRAFT ===
      {previous_output[draft]}

      For each issue: quote the passage, explain the problem, suggest the fix.

  # Wave 4: Waits for BOTH reviews before applying fixes.
  - id: apply-fixes
    name: "Apply Fixes"
    description: "Revise the draft incorporating all reviewer feedback."
    task_type: content
    model_tier: sonnet
    thinking_level: medium
    depends_on:
      - draft
      - flow-review              # Must wait for both reviewers before rewriting.
      - red-team
    timeout_minutes: 40
    prompt_template: |
      Revise this article by addressing ALL feedback from both reviewers.

      === ORIGINAL DRAFT ===
      {previous_output[draft]}

      === FLOW REVIEW FEEDBACK ===
      {previous_output[flow-review]}

      === ADVERSARIAL REVIEW FEEDBACK ===
      {previous_output[red-team]}

      Maintain the original tone ({input[tone]}) and approximate word count.
      Output only the revised article text.

    # human_review: true would pause the pipeline here and wait for approval.
    # Uncomment for production use with a real editorial workflow:
    # human_review: true
```

---

## 4. config_schema Reference

`config_schema` is a [JSON Schema](https://json-schema.org/) object that describes what inputs your template accepts. It drives:

- **`orch start`** — interactive input wizard (prompts user for each property)
- **`orch validate`** — checks that required fields are present and types match
- **`orch templates info`** — displays the input contract to potential users
- **`example_input`** — wizard uses `default` values as suggestions

### 4.1 Top-Level Structure

```yaml
config_schema:
  type: object         # Always "object" for template inputs
  properties:          # Map of field_name → field_schema
    field_name:
      type: string     # string | integer | number | boolean | array | object
      description: "Shown in wizard and orch templates info"
      default: "value" # Pre-fills the wizard prompt
  required:            # Fields that must be provided at runtime
    - field_name
```

### 4.2 Field Types and Options

```yaml
config_schema:
  type: object
  properties:

    # ── String ──────────────────────────────────────────────────────────────
    topic:
      type: string
      description: "Article topic"
      default: "AI in healthcare"
      minLength: 5
      maxLength: 500

    # ── Enumerated string (renders as dropdown in wizard) ────────────────────
    tone:
      type: string
      description: "Writing tone"
      enum: [professional, conversational, academic, technical]
      default: professional

    # ── Integer ─────────────────────────────────────────────────────────────
    word_count:
      type: integer
      description: "Target word count"
      minimum: 500
      maximum: 5000
      default: 1500

    # ── Number (float) ───────────────────────────────────────────────────────
    temperature:
      type: number
      description: "Model temperature override"
      minimum: 0.0
      maximum: 1.0
      default: 0.7

    # ── Boolean ──────────────────────────────────────────────────────────────
    include_sources:
      type: boolean
      description: "Include a sources section in the output"
      default: true

    # ── Array of strings ─────────────────────────────────────────────────────
    keywords:
      type: array
      description: "Target SEO keywords"
      items:
        type: string
      minItems: 1
      maxItems: 10

    # ── Array of enumerated strings ──────────────────────────────────────────
    source_types:
      type: array
      description: "Types of sources to gather"
      items:
        type: string
        enum: [academic, news, expert, government]
      default: [academic, news]

    # ── Formatted string ─────────────────────────────────────────────────────
    publish_date:
      type: string
      format: date           # date | date-time | uri | email
      description: "Target publication date (YYYY-MM-DD)"

    # ── Nested object ────────────────────────────────────────────────────────
    author:
      type: object
      description: "Author details"
      properties:
        name:
          type: string
        email:
          type: string
          format: email
      required: [name]

  required:
    - topic
```

### 4.3 Form Rendering Hints

The `orch start` wizard uses these schema properties for rendering:

| Schema property | Wizard behaviour |
|---|---|
| `enum` | Renders as a numbered menu (pick one) |
| `type: boolean` | Renders as `[y/n]` prompt |
| `default` | Shown as the suggested value; press Enter to accept |
| `description` | Shown as the prompt label |
| `required` | Wizard will not proceed without a value |

---

## 5. Variable Interpolation Guide

Prompts use Python `str.format()`-style placeholders (`{variable}` or `{variable[key]}`). The engine substitutes these before sending the prompt to the model.

### 5.1 Available Variables

| Variable | Resolves to | Example |
|---|---|---|
| `{input}` | The full initial input dict (as a string) | `{input}` |
| `{input[field]}` | A specific field from the initial input | `{input[topic]}` |
| `{previous_output[phase_id]}` | Full result dict of a completed phase (includes state, metadata, tokens) | `{previous_output[research]}` |
| `{previous_output}` | All accumulated phase outputs (full dict) | `{previous_output}` |
| `{config[key]}` | A value from the pipeline-level config dict (set at pipeline init, separate from input) | `{config[tone]}` |
| `{phase_id.output}` | **Clean text only** from a phase's output (recommended) | `{research.output}` |
| `{phase_id}` | Same as `{phase_id.output}` — clean text shorthand | `{research}` |
| `{skill_context[name]}` | Content injected from a `skill_refs` file | `{skill_context[style_guide]}` |

> **⚠️ `{phase_id.output}` vs `{previous_output[phase_id]}`:** These are NOT equivalent. `{research.output}` gives you **clean text only** (extracted from the result). `{previous_output[research]}` gives the **full TaskResult dict** including state, confidence, metadata, and tokens. **Prefer `{phase_id.output}` or `{phase_id}` for prompt chaining** — it's cleaner and what downstream agents actually need.

> **Missing-key behaviour (fail-fast):** In real runs (`standalone` / `openrouter` / `openclaw` / `claudecode`), if a referenced `{config[...]}`, `{input[...]}`, or `{previous_output[...]}` key is missing, the engine **rejects the phase before dispatch** (terminal `permanently_failed`) and names every `<MISSING:...>` marker (issues #535/#676). **Dry-run is the exception** — it logs the markers and proceeds so you can smoke-test structure (issue #659). So `<MISSING:...>` is a dry-run-only diagnostic; a real run never silently dispatches one.

### 5.2 Common Patterns

**Referencing initial input:**
```yaml
prompt_template: |
  Write an article about: {input[topic]}
  Target audience: {input[audience]}
  Word count: {input[word_count]}
```

**Chaining phase outputs:**
```yaml
prompt_template: |
  === RESEARCH ===
  {previous_output[research]}

  === OUTLINE ===
  {previous_output[outline]}

  Now write the full article.
```

**Using the alternate dot-notation (code-development style):**
```yaml
prompt_template: |
  ## Requirements
  {requirements.output}

  ## Implementation
  {implement.output}

  Review this code critically.
```

**Injecting skill context:**
```yaml
skill_refs:
  - skills/style-guide.md   # resolved relative to template directory

prompt_template: |
  Follow these style guidelines exactly:
  {skill_context[style-guide]}

  Now write: {input[topic]}
```

**Referencing entire input dict (for flexible prompts):**
```yaml
prompt_template: |
  Use the following task details:
  {input}

  Produce a requirements document.
```

### 5.3 Interpolation Scope Per Phase

Each phase only has access to outputs from phases that have **already completed** by the time it runs. A phase in Wave 3 can reference Wave 1 and Wave 2 outputs but not other Wave 3 outputs (they run concurrently).

```
Wave 1: [research]                        → only {input} available
Wave 2: [outline, draft]                  → + {previous_output[research]}
Wave 3: [flow-review, red-team]           → + {previous_output[outline]}, {previous_output[draft]}
Wave 4: [apply-fixes]                     → + all previous outputs
```

---

## 6. Cookbook Patterns

### 6.1 Linear Pipeline (A → B → C)

The simplest pattern. Each phase depends on the previous one. Use when each step must incorporate the full output of the prior step.

```yaml
id: linear-pipeline
name: "Linear Pipeline"
version: "1.0.0"
description: "Sequential A → B → C pipeline."
author: "You"

phases:
  - id: step-a
    name: "Step A"
    task_type: research
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: |
      Research this topic thoroughly: {input[topic]}

  - id: step-b
    name: "Step B"
    task_type: content
    model_tier: sonnet
    thinking_level: medium
    depends_on: [step-a]             # Waits for step-a
    prompt_template: |
      Using this research:
      {previous_output[step-a]}

      Write a first draft about: {input[topic]}

  - id: step-c
    name: "Step C"
    task_type: review
    model_tier: sonnet
    thinking_level: low
    depends_on: [step-b]             # Waits for step-b (and transitively step-a)
    prompt_template: |
      Review and polish this draft:
      {previous_output[step-b]}
```

**Execution order:** `step-a` → `step-b` → `step-c`

---

### 6.2 Diamond Dependency (A → B, C → D)

Two parallel phases (B and C) each depend on A, then converge at D. Use when two independent analyses can run concurrently and their results must be synthesised.

```yaml
id: diamond-pipeline
name: "Diamond Pipeline"
version: "1.0.0"
description: "A → B,C (parallel) → D (convergence)."
author: "You"

phases:
  - id: parse
    name: "Parse Input"
    task_type: code
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: |
      Parse and summarise the input for downstream analysis: {input[content]}

  - id: security-check
    name: "Security Analysis"
    task_type: review
    model_tier: sonnet
    thinking_level: medium
    depends_on: [parse]              # Depends on parse; independent of style-check
    prompt_template: |
      Run a security analysis on this parsed content:
      {previous_output[parse]}

  - id: style-check
    name: "Style Analysis"
    task_type: review
    model_tier: sonnet
    thinking_level: low
    depends_on: [parse]              # Also depends on parse; runs in same wave as security-check
    prompt_template: |
      Run a style and convention analysis on this parsed content:
      {previous_output[parse]}

  - id: report
    name: "Synthesise Report"
    task_type: content
    model_tier: sonnet
    thinking_level: low
    depends_on:
      - security-check               # Waits for BOTH parallel phases
      - style-check
    prompt_template: |
      Combine these two reviews into a single prioritised report.

      === SECURITY ===
      {previous_output[security-check]}

      === STYLE ===
      {previous_output[style-check]}
```

**Execution order:** `parse` → [`security-check`, `style-check`] → `report`

---

### 6.3 Human Review Gate (`human_review: true`)

Pause the pipeline after a critical phase and wait for a human to approve the output before continuing. The pipeline resumes only after the reviewer signals approval.

```yaml
id: gated-pipeline
name: "Human-Gated Pipeline"
version: "1.0.0"
description: "Research → draft → HUMAN GATE → publish-prep."
author: "You"

phases:
  - id: research
    name: "Research"
    task_type: research
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: |
      Research: {input[topic]}

  - id: draft
    name: "Draft"
    task_type: content
    model_tier: sonnet
    thinking_level: medium
    depends_on: [research]
    prompt_template: |
      Write a draft based on:
      {previous_output[research]}

  # ── Human review gate ────────────────────────────────────────────────────
  # The pipeline pauses here. A human reads the draft, approves or rejects.
  # Only approved drafts proceed to publish-prep.
  - id: human-gate
    name: "Editorial Approval"
    task_type: review
    model_tier: sonnet
    thinking_level: off
    human_review: true             # ← GATE: pipeline pauses until approval
    depends_on: [draft]
    timeout_minutes: 2880          # 48 hours before auto-expiry
    prompt_template: |
      Summarise the draft for the human reviewer.

      === DRAFT TO REVIEW ===
      {previous_output[draft]}

      Provide: a 3-bullet summary, confidence score (0-10), and any flags.

  - id: publish-prep
    name: "Publish Preparation"
    task_type: content
    model_tier: haiku
    thinking_level: off
    depends_on: [human-gate]       # Only runs after human approval
    prompt_template: |
      Prepare this approved draft for publication.
      Add metadata, tags, and format for the CMS.

      === APPROVED DRAFT ===
      {previous_output[draft]}
```

---

### 6.4 Multi-Model Pipeline (Haiku for speed, Opus for review)

Use cheaper, faster models for bulk or simple tasks and escalate to Opus only where complex reasoning is genuinely needed.

```yaml
id: tiered-model-pipeline
name: "Multi-Model Pipeline"
version: "1.0.0"
description: "Haiku for fast extraction → Sonnet for synthesis → Opus for final review."
author: "You"

phases:
  # Haiku: fast and cheap for classification / extraction
  - id: classify
    name: "Topic Classification"
    task_type: research
    model_tier: haiku              # Fast, cheap — suitable for structured extraction
    thinking_level: off            # No extended thinking needed
    depends_on: []
    timeout_minutes: 10
    prompt_template: |
      Classify this article into one of: [technology, health, finance, politics, science].
      Article: {input[article_text]}
      Output JSON: {"category": "...", "confidence": 0.0-1.0}

  # Haiku: fast keyword extraction
  - id: extract-keywords
    name: "Keyword Extraction"
    task_type: research
    model_tier: haiku
    thinking_level: off
    depends_on: []                 # Runs in parallel with classify (same wave)
    timeout_minutes: 10
    prompt_template: |
      Extract the top 10 keywords from this text.
      Article: {input[article_text]}
      Output JSON: {"keywords": ["...", ...]}

  # Sonnet: moderate reasoning for synthesis
  - id: summarise
    name: "Summarise"
    task_type: content
    model_tier: sonnet             # Balanced quality/cost for writing tasks
    thinking_level: low
    depends_on: [classify, extract-keywords]
    timeout_minutes: 20
    prompt_template: |
      Write a 200-word summary for a {previous_output[classify]} article.
      Keywords to include: {previous_output[extract-keywords]}
      Article: {input[article_text]}

  # Opus: deep reasoning for the final quality gate
  - id: quality-review
    name: "Quality Gate"
    task_type: review
    model_tier: opus               # Opus for rigorous final review only
    thinking_level: high           # Full extended thinking for nuanced judgment
    depends_on: [summarise]
    timeout_minutes: 30
    prompt_template: |
      You are a senior editor. Critically assess this summary.

      Original article category: {previous_output[classify]}
      Summary: {previous_output[summarise]}

      Score (0-10) on: accuracy, clarity, keyword coverage, tone.
      List any mandatory corrections before publication.
```

---

### 6.5 Skill-Augmented Phases (`skill_refs`)

Inject reusable knowledge files (style guides, tone documents, domain glossaries) into phase prompts without embedding them in the template itself.

**Directory layout:**
```
my-template.yaml
skills/
  brand-voice.md
  legal-disclaimer.md
~/.orch/skills/
  ap-style-guide.md     ← global skills, available to all templates
```

**Template:**
```yaml
id: branded-content-pipeline
name: "Branded Content Pipeline"
version: "1.0.0"
description: "Content creation with brand voice and legal compliance injected."
author: "You"

phases:
  - id: research
    name: "Research"
    task_type: research
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: |
      Research this topic: {input[topic]}

  - id: draft
    name: "On-Brand Draft"
    task_type: content
    model_tier: sonnet
    thinking_level: medium
    depends_on: [research]
    skill_refs:
      - skills/brand-voice.md        # Relative to template directory
      - ap-style-guide.md            # Resolved from ~/.orch/skills/
    prompt_template: |
      Write an article following these brand guidelines exactly:

      === BRAND VOICE GUIDE ===
      {skill_context[brand-voice]}

      === AP STYLE GUIDE ===
      {skill_context[ap-style-guide]}

      === RESEARCH ===
      {previous_output[research]}

      Topic: {input[topic]}
      Tone: {input[tone]}

  - id: compliance-check
    name: "Legal Compliance Check"
    task_type: review
    model_tier: sonnet
    thinking_level: low
    depends_on: [draft]
    skill_refs:
      - skills/legal-disclaimer.md   # Legal boilerplate and restricted terms
    prompt_template: |
      Review this draft against the legal requirements:

      === LEGAL REQUIREMENTS ===
      {skill_context[legal-disclaimer]}

      === DRAFT ===
      {previous_output[draft]}

      Flag any violations. Suggest corrections.
```

> **Skill file naming:** The key in `{skill_context[name]}` is the **file stem** (filename without extension). `skills/brand-voice.md` → `{skill_context[brand-voice]}`.

---

### 6.6 Code Generation with File Writing (`write_files`)

For coding pipelines, phases can write files directly to a working directory. The sequencer parses `FILE` blocks from the model output and extracts them.

```yaml
id: coding-pipeline
name: "Coding Pipeline"
version: "1.0.0"

git:
  enabled: true
  branch_pattern: "feat/{pipeline_id}-{run_id}"
  merge_gate: true

phases:
  - id: implement
    name: "Implement"
    task_type: code
    model_tier: sonnet
    thinking_level: medium
    depends_on: [spec]
    write_files: true              # ← extract FILE blocks and write to disk
    working_dir: "."               # relative to the repo root
    base_dir: "src/"               # safety: refuse writes outside src/
    prompt_template: |
      Implement the following specification:
      {spec.output}

      Write the code as FILE blocks.
```

---

### 6.7 Supervisor Review Loops

A supervisor LLM evaluates phase output after execution. If the verdict is REVISE, the phase re-executes with the supervisor's feedback injected. If ABORT, the pipeline fails.

```yaml
phases:
  - id: draft
    name: "Draft Article"
    task_type: content
    model_tier: sonnet
    thinking_level: medium
    depends_on: [research]
    supervisor: true                    # ← enable supervisor hook
    supervisor_model: opus              # supervisor uses Opus for judgment
    supervisor_max_retries: 2           # up to 2 REVISE cycles
    supervisor_rubric: |
      Evaluate the article on:
      1. Factual accuracy (cite sources)
      2. Logical coherence (clear argument flow)
      3. Engagement (compelling opening, strong close)
      4. Technical depth (appropriate for the audience)
      Score each 0-25. Total ≥ 75 = APPROVE, 50-74 = REVISE, <50 = ABORT.
    prompt_template: |
      Write an article based on:
      {previous_output[research]}
```

---

### 6.8 Protected Outputs (`file_guard.py`)

Protect critical files from tampering between phases. Used in behavioral trust pipelines where acceptance tests must not be modified by the implementing agent.

```yaml
phases:
  - id: acceptance_test
    name: "Write Acceptance Tests"
    task_type: code
    model_tier: sonnet
    depends_on: [spec]
    write_files: true
    protected_outputs:              # ← SHA256-sealed after this phase
      - acceptance_tests.py

  - id: implement
    name: "Implement Code"
    task_type: code
    model_tier: sonnet
    depends_on: [acceptance_test]
    write_files: true
    # file_guard verifies acceptance_tests.py hash hasn't changed
    prompt_template: |
      Implement the spec. Do NOT modify acceptance_tests.py.
      {spec.output}
```

Hash verification is automatic and silent — the implementing agent never knows the seal exists.

---

### 6.9 State Machine Transitions

For pipelines with conditional routing (review → fix loops, retry paths), use transitions instead of linear `depends_on` chains. The `StateMachineSequencer` is automatically selected when any phase has `transitions`.

```yaml
default_transitions:                # applied to all phases as a baseline
  success: _next                   # _next = proceed to next phase in order
  failed: _abort                   # _abort = stop the pipeline

max_iterations: 10                  # safety limit for transition loops

phases:
  - id: implement
    name: "Implement"
    task_type: code
    model_tier: sonnet
    depends_on: []
    prompt_template: "..."

  - id: review
    name: "Code Review"
    task_type: review
    model_tier: opus
    depends_on: [implement]
    supervisor: true
    transitions:                    # ← phase-specific routing
      approve: final               # APPROVE verdict → skip to final
      revise: implement            # REVISE → loop back to implement
      abort: _abort                # ABORT → stop pipeline
    prompt_template: "..."

  - id: final
    name: "Final Output"
    task_type: content
    model_tier: haiku
    depends_on: [review]
    prompt_template: "..."
```

**Special transition targets:**
- `_next` — proceed to the next phase in declaration order
- `_abort` — stop the pipeline with a failure status
- Any phase ID — jump to that phase directly

---

### 6.10 Budget-Controlled Pipelines

Prevent runaway costs by setting per-run and per-day limits.

```yaml
id: budget-aware-pipeline
name: "Budget-Controlled Pipeline"

budget:
  max_cost_per_run: 5.00          # abort if this run exceeds $5
  max_cost_per_day: 50.00         # reject new launches if daily spend ≥ $50
  warn_at_percentage: 80.0        # log a warning at 80% of limit

phases:
  # ...
```

---

### 6.11 Pipeline Chaining (`on_complete`)

Trigger child pipelines automatically based on the parent's outcome.

```yaml
id: code-with-deploy
name: "Code + Auto-Deploy"

on_complete:
  success:
    - template: deploy-staging
      input_map:
        artifact_path: "{{output_dir}}/build"
        commit_sha: "{{git.commit_sha}}"
  failed:
    - template: alert-team
      input_map:
        error: "{{final_output.errors}}"
        run_id: "{{run_id}}"

phases:
  # ...
```

`{{placeholder}}` values are interpolated from the parent run's context at chain time. The `max_chain_depth` setting (default 5, hard limit 20) prevents infinite recursion.

---

## 7. Testing Your Template

### 7.1 Validate Structure

Check for missing fields, duplicate IDs, unknown `depends_on` references, and dependency cycles:

```bash
# Basic structural validation
orch validate my-pipeline.yaml

# Extended linting (also checks model_tier, thinking_level, prompt refs, docs fields)
orch validate my-pipeline.yaml
```

Example output:
```
✓ Template 'my-pipeline.yaml' is valid (5 phases)

# Or with errors:
✗ Template 'my-pipeline.yaml' has 2 error(s):
  • Phase 'write' depends on unknown phase 'outline'
  • Missing required documentation field: 'author'
  ⚠ 1 warning(s):
  • Phase 'draft' has unknown model_tier='gpt4'; did you mean 'opus'?
```

### 7.2 Preview Execution Order

Inspect the execution plan (waves) before running:

```bash
orch list-phases my-pipeline.yaml
```

```
Pipeline: 'My Pipeline'  (v1.0.0)
Phases: 5  |  Waves: 4

  Wave 1
    ├─ research        model=sonnet    thinking=low    deps=[none]
    ├─ outline         model=sonnet    thinking=low    deps=[none]

  Wave 2
    ├─ draft           model=sonnet    thinking=medium deps=[research, outline]

  Wave 3
    ├─ flow-review     model=sonnet    thinking=low    deps=[draft]
    ├─ red-team        model=sonnet    thinking=medium deps=[draft]

  Wave 4
    ├─ apply-fixes     model=sonnet    thinking=medium deps=[draft, flow-review, red-team]
```

### 7.3 Dry Run (No API Key Required)

Validate the full execution path and variable interpolation without calling any model:

```bash
orch run my-pipeline.yaml --mode dry-run --input '{"topic": "AI safety", "tone": "professional"}'
```

The dry-run executor:
- Resolves all variable interpolations and **reports** any `<MISSING:key>` placeholders (dry-run tolerates them so you can test structure; a real run would instead fail the phase — see §5.1)
- Executes all phases with mock outputs
- Validates output schemas if defined
- Reports the final execution graph and any warnings

### 7.4 Scenario Testing

Write a YAML scenario file to test your template against expected outputs:

```yaml
# scenarios/my-pipeline-smoke.yaml
template: my-pipeline.yaml
scenarios:
  - name: "Basic happy path"
    input:
      topic: "Renewable energy trends"
      tone: "professional"
      word_count: 1000
    assertions:
      - phase: draft
        check: contains
        value: "renewable"
      - phase: apply-fixes
        check: min_length
        value: 500
      - phase: apply-fixes
        check: confidence_above
        value: 0.7
```

```bash
orch scenario run scenarios/my-pipeline-smoke.yaml
```

---

### 7.4 Timeout Best Practices

**Always set explicit `timeout_minutes` per phase.** The default is 30 minutes, which means a 5-phase pipeline has a 2.5-hour ceiling even if phases finish in 5 minutes each.

Guidelines from production use:
| Phase type | Recommended timeout |
|---|---|
| Research / analysis | 10 min |
| Implementation (code generation) | 15 min |
| Code review (Opus with high thinking) | 15 min |
| Apply fixes | 10 min |
| Test generation | 10 min |
| Content writing | 15 min |
| Human review gate | 2880 min (48h) |

**Key insight:** If a phase consistently times out, the prompt is too broad. Tighten the spec instead of increasing the timeout. A focused 10-minute agent produces better output than a wandering 30-minute one.

**OpenClaw mode note:** When running via `orch run --mode openclaw`, the pipeline process itself must stay alive for the full duration. Use `nohup` or a process manager for pipelines expected to run >10 minutes.

---

### 7.5 Configuring Graders for Scenario Testing

Graders evaluate pipeline output quality. Attach them via scenario YAML files.

**Available graders:**

| Grader | Use for | Config |
|--------|---------|--------|
| `keyword` | Structural checks (required terms, format) | `keywords: [list]`, `min_matches: N` |
| `llm_judge` | Quality assessment (coherence, accuracy) | `rubric: <text>`, `model: <model-id>` |
| `regex` | Pattern matching (dates, URLs, code blocks) | `pattern: <regex>`, `must_match: true` |

**Example scenario with graders:**

```yaml
id: my-pipeline-quality-check
template: my-pipeline.yaml
input:
  task: "Summarize recent AI developments"

acceptance:
  - criterion: "Contains key topics"
    grader: keyword
    weight: 30
    config:
      keywords: ["transformer", "language model", "training"]
      min_matches: 2

  - criterion: "Output is coherent and well-structured"
    grader: llm_judge
    weight: 50
    config:
      rubric: |
        Score the output on:
        1. Logical flow (0-25)
        2. Factual accuracy (0-25)
        3. Completeness (0-25)
        4. Clarity (0-25)
      model: "anthropic/claude-sonnet-4-6"

  - criterion: "Includes at least one source URL"
    grader: regex
    weight: 20
    config:
      pattern: "https?://[\\w./\\-]+"
      must_match: true

pass_threshold: 70
```

**Run with:** `orch scenario run scenarios/my-check.yaml --mode dry-run`

**Writing custom graders:** Implement the `Grader` base class in `scenario_runner/graders/`. See `keyword_grader.py` for a minimal example (~50 lines).

---

## 8. Troubleshooting

### 8.1 Common Errors

**`Template missing required field 'id'`**
- Every template must have `id` and `name` at the top level.
- Check your YAML indentation — these must be at the root level, not nested.

```yaml
# ✗ Wrong — 'id' is nested inside a key
template:
  id: my-pipeline

# ✓ Correct — 'id' is at root level
id: my-pipeline
```

**`Phase 'X' depends on unknown phase 'Y'`**
- A `depends_on` reference points to a phase ID that doesn't exist.
- Check for typos: phase IDs are case-sensitive (`research` ≠ `Research`).
- Check that the phase isn't accidentally commented out.

**`Duplicate phase ID 'X'`**
- Two phases share the same `id`. All phase IDs must be unique within a template.
- Common cause: copy-pasting a phase block without changing the `id`.

**`Cycle detected involving phase(s): ['A', 'B']`**
- Phases A and B form a circular dependency (A depends on B, B depends on A).
- Draw the dependency graph on paper to find the cycle.
- Fix by removing one dependency or inserting a new intermediate phase.

**`skill_ref file not found: 'skills/my-skill.md'`**
- The engine looked in: (1) the template's directory, (2) `~/.orch/skills/`.
- Check the path is correct relative to the template file.
- Check the file exists and isn't empty.
- Check for path traversal sequences (`../`) — these are rejected for security.

### 8.2 Variable Interpolation Issues

**`<MISSING:topic>` appears in model output**
- **Cause:** a referenced key (`{input[...]}`, `{config[...]}`, or `{previous_output[...]}`) wasn't provided. **In a real run this no longer reaches the model** — the engine fails the phase before dispatch and names the marker (#535/#676). If you see `<MISSING:topic>` it means you ran in **dry-run**, which tolerates it. **Fix:** supply the key via `--input` / `--input-file` / `config_schema` default, or correct the reference.

**`{previous_output[phase-id]}` returns empty or `{}`**
- The referenced phase ran but produced no parseable output.
- Check the phase's prompt: does it ask the model to return structured JSON?
- In dry-run mode, phase outputs are mocks — use `orch run --mode standalone` for real output.
- Phase IDs with hyphens must match exactly: `{previous_output[flow-review]}` not `{previous_output[flow_review]}`.

**Curly brace literals in prompts are mangled**
- `str.format()` treats `{` and `}` as interpolation markers.
- To include literal braces in your prompt (e.g. for JSON examples), double them:

```yaml
prompt_template: |
  Output JSON in this format: {{"key": "value", "count": 0}}
  # Renders as: Output JSON in this format: {"key": "value", "count": 0}
```

### 8.3 Timeout Issues

**`Phase 'X' timed out after 30 minutes`**
- Default `timeout_minutes` is 30. Research and drafting phases often need more.
- Increase `timeout_minutes` for expensive phases:

```yaml
- id: draft
  timeout_minutes: 60    # Give writing phases extra headroom
```

- Consider whether the prompt is requesting too much in one phase. Split into two phases.
- `thinking_level: high` (32,768 thinking tokens) adds significant latency — use only when necessary.

### 8.4 Model and Quality Issues

**Phase produces low-quality output consistently**
- Upgrade `model_tier` from `haiku` → `sonnet` or `sonnet` → `opus`.
- Increase `thinking_level` from `off` → `low` or `low` → `medium`.
- Make the prompt more specific: add explicit output format requirements, examples, and constraints.
- Add `output_schema` — even informal schema helps the model understand what's expected.

**Pipeline fails on retry with `permanently_failed`**
- Default `max_retries` is 3. The engine escalates the model tier on each retry.
- If all three retries fail, check the prompt for ambiguity or impossible requirements.
- Use `orch dead-letter` to inspect the failure reason and full error context.
- Use `orch retry <task-id>` to manually re-queue after fixing the prompt.

### 8.5 Validation Warnings (Extended Mode)

These are non-blocking but indicate best-practice issues:

| Warning | Fix |
|---|---|
| `model_tier='gpt4'` unknown | Use `haiku`, `sonnet`, or `opus` |
| `thinking_level='max'` unknown | Use `off`, `low`, `medium`, or `high` |
| `Recommended field 'use_cases' missing` | Add `use_cases:` list to template header |
| `Recommended field 'example_input' missing` | Add `example_input:` dict for wizard UX |
| `version does not match semver` | Use `X.Y.Z` format, e.g. `"1.0.0"` |
| `config_schema missing 'properties'` | Add `properties:` block when `type: object` |
| `Phase 'X' references unknown phase 'Y' in prompt` | Fix or remove `{Y.output}` references |

---

## Pitfalls & Pre-PR Checklist

The mistakes that most often bounce a template PR — and the authoring hygiene that prevents them.

**Run `orch validate` before opening a PR.** Unvalidated templates are the most common reason a PR is sent back. `orch validate <template>` (and `orch validate <template> --fix` for simple auto-corrections, see §7.1) catches missing required fields, bad `depends_on` references, and structural errors before review.

**`config_schema` defaults vs `command` phases.** A `config_schema` default is *not* injected into a `command`-type phase's `command` / `working_dir` interpolation the way runtime input is. Make sure every `{config[...]}` / `{input[...]}` a command phase references is actually populated at run time — otherwise the missing-key guard fails the phase before dispatch (see the [Missing-key behaviour](#51-available-variables) note in §5.1).

**`max_iterations` on `request_changes` / review loops.** Any loop where a reviewer can send work back — `review ↔ fix`, `spec ↔ spec_adversary`, supervisor loops — needs a bounded `max_iterations` (the loop's max rounds). Without it, a non-converging verdict loops until the budget is exhausted. `max_iterations` is a real phase/transition field (documented in §2.2 and §6.9); set it deliberately. (`coding-pipeline-skip-spec`, for example, caps its loops at 5 / 6 / 3 rounds.)

**Current `<MISSING:>` fail-fast.** In real runs the engine rejects a phase before dispatch when a referenced key is missing and names each `<MISSING:...>` marker (#535/#676); only dry-run tolerates them. See §5.1 — do not rely on the old silent-substitution behavior.

**Bare `#N` autolinking.** A bare `#123` written into a prompt, description, or PR body is auto-linked to issue 123 wherever GitHub renders it. If you mean a literal hash-number (e.g. "phase #3"), rephrase or escape it to avoid an accidental cross-link. This is authoring hygiene, not an engine behavior.

---

## Appendix: Template Checklist

Use this checklist before publishing or sharing a template:

```
Template Header
  [ ] id — unique, kebab-case
  [ ] name — clear, human-readable
  [ ] version — semver X.Y.Z
  [ ] description — what the pipeline does (1 paragraph)
  [ ] author — your name
  [ ] category — appropriate grouping
  [ ] tags — searchable labels
  [ ] use_cases — 2–4 real-world scenarios
  [ ] example_input — minimal working example

Input Schema
  [ ] config_schema defined with type: object
  [ ] All required inputs listed in required: []
  [ ] Descriptions written for every property
  [ ] Defaults set for optional fields
  [ ] Enum constraints used for fixed-choice fields

Phases
  [ ] Every phase has id and name
  [ ] All depends_on references point to real phase IDs
  [ ] No duplicate phase IDs
  [ ] prompt_template uses {input[field]} correctly
  [ ] Phase outputs referenced with {previous_output[phase-id]}
  [ ] Literal { } in prompts escaped as {{ }}
  [ ] model_tier chosen for the task complexity (haiku/sonnet/opus)
  [ ] thinking_level appropriate for the task
  [ ] timeout_minutes generous enough for the expected model latency
  [ ] skill_refs files exist and are within allowed directories

Advanced Features (if used)
  [ ] parallel / max_parallel / fail_fast set appropriately
  [ ] budget limits configured if running autonomously
  [ ] git block configured for coding pipelines
  [ ] on_complete chains tested (check max_chain_depth)
  [ ] supervisor phases have rubric and reasonable max_retries
  [ ] write_files phases have base_dir safety boundary
  [ ] protected_outputs listed for tamper-sensitive files
  [ ] transitions do not create infinite loops (check max_iterations)
  [ ] routing_config tiers have non-overlapping score ranges
  [ ] scenario path set for templates that require auto-scoring

Testing
  [ ] orch validate passes (0 errors)
  [ ] orch validate passes or warnings are understood
  [ ] orch list-phases shows expected execution waves
  [ ] orch run --mode dry-run reports no <MISSING:*> placeholders (any that appear would fail the phase in a real run — #535/#676)
  [ ] At least one live run tested with real input
```
