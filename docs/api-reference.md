# API Reference

> **Audience:** Developers integrating with or extending the orchestration engine. Covers the CLI, core Python classes, and template YAML schema.

---

## CLI Commands

The `orch` entry point is installed by `pip install orchestration-engine`. All commands accept `--db-path <path>` (custom database location) and `-v`/`--verbose` (debug logging) as global options.

```
Usage: orch [OPTIONS] COMMAND [ARGS]...
```

---

### `orch submit` — Submit a task

Queue a new task for execution.

```bash
orch submit \
  --type content \
  --payload '{"prompt": "Write a 500-word summary of remote work trends"}' \
  --priority high \
  --max-retries 3 \
  --timeout 1800 \
  --min-confidence 0.8 \
  --cost-limit 0.50 \
  --orchestra-id "my-pipeline-run-001" \
  --orchestra-phase "write" \
  --tag linkedin --tag article \
  --created-by "conny"
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `--type` | choice | required | Task type: `content`, `code`, `research`, `translation`, `review` |
| `--payload` | JSON string | required | Task-specific input data as a JSON object |
| `--priority` | choice | `normal` | `critical`, `high`, `normal`, `low` |
| `--max-retries` | int | `3` | Maximum automatic retry attempts |
| `--timeout` | int | `3600` | Timeout in seconds |
| `--min-confidence` | float | `0.7` | Minimum acceptable confidence score (0.0–1.0) |
| `--cost-limit` | float | none | Maximum spend in USD for this task |
| `--orchestra-id` | string | none | Associate with a pipeline run |
| `--orchestra-phase` | string | none | Phase name within the pipeline |
| `--tag` | string (repeatable) | none | Arbitrary labels for filtering |
| `--created-by` | string | none | Creator identifier for audit logs |

**Output:**
```
✓ Task submitted successfully
Task ID: 3f8a1c2d-...
Type: content
Priority: high
```

---

### `orch status` — Task or queue status

Without a task ID, shows queue-wide statistics. With a task ID, shows the full status of that specific task.

```bash
# Queue overview
orch status

# Specific task
orch status 3f8a1c2d-4e5b-6789-abcd-ef0123456789
```

**Queue overview output:**
```
Queue Statistics
├─ Timestamp: 2026-02-21 10:30:00
├─ Total Tasks: 42
├─ Queued: 5
├─ Running: 3
├─ Completed: 30
├─ Failed: 2
├─ Retrying: 1
├─ Cancelled: 1
├─ Workers: 3/8 (37.5%)
└─ Dead Letter: 0
```

**Task status output:**
```
Task Status: 3f8a1c2d-...
├─ Type: content
├─ State: running
├─ Priority: HIGH
├─ Created: 2026-02-21 10:25:00
├─ Started: 2026-02-21 10:25:05
├─ Completed: N/A
├─ Retries: 1/3
└─ Progress: 45.0%
```

---

### `orch list` — List tasks

```bash
# All tasks (default: last 20)
orch list

# Filter by state and type
orch list --state running --state queued --type content

# Filter by orchestra run, output as JSON
orch list --orchestra-id "my-pipeline-run-001" --format json

# Paginate
orch list --limit 50 --offset 50
```

**Options:**

| Option | Description |
|---|---|
| `--state` (repeatable) | Filter: `queued`, `running`, `success`, `failed`, `retry`, `permanently_failed`, `cancelled` |
| `--type` (repeatable) | Filter: `content`, `code`, `research`, `translation`, `review` |
| `--priority` (repeatable) | Filter: `critical`, `high`, `normal`, `low` |
| `--orchestra-id` | Filter by pipeline run ID |
| `--tag` (repeatable) | Filter by tag |
| `--limit` | Max results (default: 20) |
| `--offset` | Skip N results for pagination |
| `--format` | `table` (default) or `json` |

---

### `orch cancel` — Cancel a task

```bash
# With confirmation prompt
orch cancel 3f8a1c2d-...

# Skip confirmation
orch cancel --force 3f8a1c2d-...
```

Only tasks in `queued` or `running` state can be cancelled. Returns exit code 1 if the task is not in a cancellable state.

---

### `orch retry` — Retry a failed task

```bash
orch retry 3f8a1c2d-...
```

Only tasks in `failed` or `permanently_failed` state can be retried. Resets the task to `queued` and increments the retry count.

---

### `orch dead-letter` — Inspect the dead letter queue

```bash
orch dead-letter
orch dead-letter --limit 50
```

Shows tasks that permanently failed after exhausting all retries. Use this for post-mortem analysis.

**Output columns:** Original ID, Type, Failure Reason, Attempts, Failed At

---

### `orch health` — System health check

```bash
orch health
```

Checks for common problems: high queue depth (>100), high dead letter count (>50), workers present but no active workers. Prints `✅ System appears healthy` or a list of warnings.

---

### `orch execute` — Force-run a specific task immediately

```bash
orch execute 3f8a1c2d-...

# Override model and timeout
orch execute --model sonnet-4 --timeout 900 3f8a1c2d-...

# Bypass worker pool capacity check
orch execute --force 3f8a1c2d-...
```

Bypasses the normal queue and executes the task immediately. Useful for debugging or urgent one-off runs.

---

### `orch watch` — Monitor a running task

```bash
# One-shot snapshot
orch watch 3f8a1c2d-...

# Follow in real time (updates every 2s)
orch watch --follow 3f8a1c2d-...

# Custom refresh rate
orch watch -f --refresh 5 3f8a1c2d-...
```

Shows progress percentage, current model, token usage, cost, and a live event log.

---

### `orch workers` — Worker pool status

```bash
orch workers

# Show individual worker details
orch workers --detailed
```

Displays total/max workers, session utilisation, daily cost, and workers grouped by state (idle, running, stale).

---

### `orch quickstart` — Zero-config first pipeline

```bash
orch quickstart
```

Creates a `hello-pipeline.yaml` in the current directory and runs it immediately. Designed for new users to see a working pipeline in 30 seconds with zero configuration beyond an API key.

---

### `orch start` — Interactive wizard

```bash
orch start
```

Interactive wizard that lists installed templates, lets you pick one, fills in required inputs via prompts (driven by the template's `config_schema`), and runs the pipeline. The guided alternative to `orch run`.

---

### `orch templates` — Browse and manage templates

```bash
orch templates list          # Show installed templates
orch templates info <name>   # Show template details, phases, inputs
orch templates install <src> # Install from file or URL
orch templates uninstall <n> # Remove an installed template
```

Browse, inspect, install, and remove pipeline templates. `list` shows name, version, phase count, and description. `info` shows the full phase execution plan and config schema.

---

### `orch run` — Run a pipeline template

```bash
orch run path/to/template.yaml [--input key=value ...]
```

Loads a YAML template, resolves dependencies, and executes all phases end-to-end via the AnthropicExecutor. Outputs are saved to `output/<template>-<timestamp>/`. Supports `--dry-run` to preview without executing.

---

### `orch validate` — Validate a template file

```bash
orch validate path/to/template.yaml
```

Checks for structural errors: missing required fields, duplicate phase IDs, unknown `depends_on` references, and dependency cycles. Exits with code 0 if valid.

```
✓ Template 'content-pipeline.yaml' is valid (5 phases)
```

```
✗ Template 'bad-template.yaml' has 2 error(s):
  • Phase 'write' depends on unknown phase 'outline'
  • Duplicate phase ID 'research' (first at index 0, again at index 3)
```

---

### `orch list-phases` — Show execution plan

```bash
orch list-phases path/to/template.yaml
```

Prints the topologically sorted execution plan: phases grouped into waves, with their model tier, thinking level, and dependencies.

```
Pipeline: 'Content Pipeline'  (v1.0.0)
Phases: 5  |  Waves: 5

  Wave 1
    ├─ research                       model=sonnet    thinking=low    deps=[none]

  Wave 2
    ├─ write                          model=sonnet    thinking=low    deps=[research]

  Wave 3
    ├─ fact_check                     model=sonnet    thinking=medium deps=[write, research]

  Wave 4
    ├─ apply_fixes                    model=sonnet    thinking=low    deps=[write, fact_check]

  Wave 5
    ├─ final_output                   model=sonnet    thinking=low    deps=[apply_fixes]
```

Phases in the same wave are independent and could theoretically run in parallel.

---

## Python Classes

### `TaskSpec`

Input specification for a new task. Passed to `TaskQueue.submit_task()`.

```python
from orchestration_engine.schemas import TaskSpec, TaskType, Priority, ModelTier

spec = TaskSpec(
    type=TaskType.CONTENT,
    payload={"prompt": "Write about renewable energy"},
    priority=Priority.NORMAL,
    max_retries=3,
    timeout_seconds=3600,
    min_confidence=0.7,
    preferred_model=ModelTier.SONNET,
    cost_limit_usd=Decimal("0.50"),
    orchestra_id="run-001",
    orchestra_phase="write",
    tags=["linkedin", "draft"],
    created_by="pipeline",
)
```

**Fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | `str` | auto UUID | Unique task identifier |
| `type` | `TaskType` | required | Task type enum value |
| `payload` | `dict` | required | Task-specific input data |
| `priority` | `Priority` | `NORMAL` | Queue priority (1=CRITICAL … 4=LOW) |
| `retry_count` | `int` | `0` | Current retry attempt number |
| `max_retries` | `int` | `3` | Maximum automatic retries |
| `timeout_seconds` | `int` | `3600` | Execution timeout |
| `min_confidence` | `float` | `0.7` | Minimum acceptable confidence (0.0–1.0) |
| `preferred_model` | `ModelTier \| None` | `None` | Override model selection |
| `cost_limit_usd` | `Decimal \| None` | `None` | Hard cost ceiling |
| `orchestra_id` | `str \| None` | `None` | Parent pipeline run ID |
| `orchestra_phase` | `str \| None` | `None` | Phase name within the pipeline |
| `tags` | `list[str]` | `[]` | Arbitrary labels |
| `created_by` | `str \| None` | `None` | Creator identifier |

---

### `TaskResult`

Complete execution result returned by executors.

```python
from orchestration_engine.schemas import TaskResult, TaskState

result: TaskResult  # returned by executor.execute(task_spec)

print(result.state)              # TaskState.SUCCESS
print(result.confidence)         # 0.85
print(result.confidence_level)   # ConfidenceLevel.HIGH (auto-computed)
print(result.result)             # {"text": "...article content..."}
print(result.tokens_consumed)    # 2341
print(result.cost_usd)           # Decimal("0.0042")
print(result.model_used)         # "claude-sonnet-4-6"
```

**Key fields:**

| Field | Type | Description |
|---|---|---|
| `task_id` | `str` | Matches the originating `TaskSpec.id` |
| `task_type` | `TaskType` | |
| `state` | `TaskState` | Final state: `SUCCESS`, `FAILED`, etc. |
| `confidence` | `float` | Quality score 0.0–1.0 |
| `confidence_level` | `ConfidenceLevel` | Auto-computed label from `confidence` |
| `result` | `dict` | Task output data |
| `errors` | `list[TaskError]` | Structured error list |
| `model_used` | `str \| None` | Actual Claude model identifier |
| `tokens_consumed` | `int` | Total input + output tokens |
| `cost_usd` | `Decimal \| None` | Estimated USD cost |
| `execution_time_seconds` | `float` | Wall-clock duration |

---

### `TaskState` — Lifecycle States

```python
class TaskState(str, Enum):
    QUEUED             = "queued"              # Waiting in queue
    RUNNING            = "running"             # Executor is active
    SUCCESS            = "success"             # Completed successfully
    FAILED             = "failed"              # Failed, may retry
    RETRY              = "retry"               # Scheduled for retry
    PERMANENTLY_FAILED = "permanently_failed"  # Max retries exhausted
    CANCELLED          = "cancelled"           # Manually cancelled
```

---

### `Priority` — Queue Priority

```python
class Priority(IntEnum):
    CRITICAL = 1   # Bypass normal queue
    HIGH     = 2   # Before NORMAL tasks
    NORMAL   = 3   # Default
    LOW      = 4   # Background work
```

Lower integer value = higher priority.

---

### `ModelTier` — Available Models

```python
class ModelTier(str, Enum):
    HAIKU  = "haiku-4-5"   # Fast, cheap; first attempt for content/research
    SONNET = "sonnet-4"    # Default for most production work
    OPUS   = "opus-4-6"    # Final-resort retry; complex reasoning
```

Automatic escalation paths (defined in `select_model_tier()`):

| Task Type | Attempt 1 | Attempt 2 | Attempt 3 |
|---|---|---|---|
| `content` | Haiku | Sonnet | Opus |
| `code` | Sonnet | Opus | Opus |
| `research` | Haiku | Sonnet | Opus |
| `translation` | Sonnet | Opus | Opus |
| `review` | Sonnet | Opus | Opus |

---

### `AnthropicExecutor`

Calls the Anthropic Messages API directly. Uses `urllib` — no external HTTP library required.

```python
from orchestration_engine.executors.anthropic_executor import AnthropicExecutor
from orchestration_engine.schemas import TaskSpec, TaskType

executor = AnthropicExecutor(api_key="sk-ant-...")
# or: set ANTHROPIC_API_KEY env var

task = TaskSpec(type=TaskType.CONTENT, payload={"prompt": "Summarise..."})
result = executor.execute(
    task,
    model_tier="sonnet",       # haiku / sonnet / opus
    thinking_level="low",      # off / low / medium / high
)
```

**Public methods:**

| Method | Signature | Description |
|---|---|---|
| `__init__` | `(api_key=None, max_tokens=4096)` | Initialise; reads `ANTHROPIC_API_KEY` if `api_key` omitted |
| `can_handle` | `(task_type: TaskType) → bool` | Always returns `True` (handles all task types) |
| `estimate_cost` | `(task: TaskSpec) → float` | Rough USD cost estimate before execution |
| `execute` | `(task, worker_id, model_tier, thinking_level) → TaskResult` | Run the task; returns `TaskResult` |

**Thinking levels and token budgets:**

| Level | Budget Tokens | Use When |
|---|---|---|
| `off` | 0 | Simple generation, no reasoning needed |
| `low` | 2,048 | Moderate complexity; most production phases |
| `medium` | 8,192 | Research, fact-checking, code review |
| `high` | 32,768 | Complex reasoning, architecture decisions |

The executor tries to parse structured JSON from the response (including from markdown code fences). If parsing fails, it returns `{"text": "<raw response text>"}`.

---

### `OpenAICompatibleExecutor`

Executor that calls any OpenAI-compatible `/v1/chat/completions` endpoint. Designed for Gemini-via-proxy, local LLMs (Ollama, LM Studio), or any other provider that speaks the OpenAI Chat Completions protocol.

```python
from orchestration_engine.openai_executor import OpenAICompatibleExecutor

executor = OpenAICompatibleExecutor(
    base_url="http://localhost:8765/v1",
    model="gemini-3-pro-preview",
    api_key="your-api-key",          # or "dummy" for unauthenticated proxies
    timeout_seconds=300,
    dry_run=False,
)

result = executor.execute(
    task="Write a one-paragraph summary of the water cycle.",
    worker_id="my-worker",
)

print(result.state)   # TaskState.SUCCESS
print(result.output)  # "The water cycle describes..."
```

**Constructor parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | str | `"http://localhost:8765/v1"` | Base URL for the OpenAI-compatible endpoint (without trailing slash) |
| `model` | str | `"gemini-3-pro-preview"` | Model name passed in the request body |
| `api_key` | str | `"dummy"` | Bearer token; use `"dummy"` for unauthenticated local proxies |
| `timeout_seconds` | int | `300` | Request timeout in seconds |
| `dry_run` | bool | `False` | When `True`, returns a mock result without making any HTTP call |

**Public methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `execute` | `(task: str, worker_id: str = "fallback", **kwargs) → ExecutorResult` | POST to the endpoint; returns `ExecutorResult` with `SUCCESS` or `FAILED` state |
| `can_handle` | `(task_type: str) → bool` | Always `True` — accepts any task type |
| `estimate_cost` | `(task: str, **kwargs) → float` | Always `0.0` — free-via-proxy assumption |

**Error codes returned in `ExecutorResult.error_code`:**

| Code | Cause |
|------|-------|
| `connection_error` | Network error or endpoint unreachable |
| `timeout` | Request exceeded `timeout_seconds` |
| `empty_response` | The endpoint returned an empty `content` field |
| `invalid_response` | JSON parse error or unexpected response shape |

---

### `FallbackHandler`

Wraps any primary executor and transparently retries through an `OpenAICompatibleExecutor` when the primary fails with a retriable error (`rate_limit`, `timeout`, or `overloaded`).

```python
from orchestration_engine.fallback import FallbackHandler
from orchestration_engine.executors.anthropic_executor import AnthropicExecutor

handler = FallbackHandler(
    primary_executor=AnthropicExecutor(api_key="sk-ant-..."),
    fallback_config={
        "base_url": "http://localhost:8765/v1",
        "model": "gemini-3-pro-preview",
        "api_key": "secret",
        "timeout_seconds": 300,
    },
)

result = handler.execute("Write a haiku about cloud APIs.")
```

If `fallback_config` is `None` (the default), `FallbackHandler` is a transparent pass-through with zero overhead.

**Retriable error codes:** `rate_limit`, `timeout`, `overloaded`. All other error codes (and all successes) are returned as-is from the primary executor.

**Constructor parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `primary_executor` | any | required | Any executor with an `execute(task, worker_id, **kwargs)` method |
| `fallback_config` | dict \| None | `None` | Config for the fallback `OpenAICompatibleExecutor`; `None` = no fallback |

**`fallback_config` keys** (all optional; mirror `OpenAICompatibleExecutor` constructor):

| Key | Default |
|-----|---------|
| `base_url` | `"http://localhost:8765/v1"` |
| `model` | `"gemini-3-pro-preview"` |
| `api_key` | `"dummy"` |
| `timeout_seconds` | `300` |

#### Configuring fallback in a template

Add a `fallback:` block at the top level of your pipeline YAML:

```yaml
id: my-pipeline
name: "My Pipeline"

fallback:
  base_url: "http://localhost:8765/v1"   # OpenAI-compatible proxy URL
  model: "gemini-3-pro-preview"          # Model name for the fallback
  api_key: "my-proxy-key"                # Auth token (or "dummy" for open proxies)
  timeout_seconds: 300                   # Fallback request timeout

phases:
  - id: draft
    name: "Draft"
    task_type: content
    model_tier: sonnet
    ...
```

When a `fallback:` block is present, the pipeline runner wraps every executor in a `FallbackHandler`. If any phase call to Anthropic returns `rate_limit`, `timeout`, or `overloaded`, the phase is automatically retried via the configured fallback endpoint — no changes to individual phases required.

---

### `TemplateEngine`

Loads YAML templates and computes execution order.

```python
from pathlib import Path
from orchestration_engine.templates import TemplateEngine

engine = TemplateEngine()  # default: ~/.orchestration-engine/templates/

template = engine.load_template(Path("my-pipeline.yaml"))
errors = engine.validate_template(template)       # [] if valid
waves = engine.get_execution_order(template)      # [["research"], ["write"], ...]
```

**Public methods:**

| Method | Signature | Description |
|---|---|---|
| `__init__` | `(templates_dir: Path = None)` | Default dir: `~/.orchestration-engine/templates/` |
| `load_template` | `(template_path: Path) → PipelineTemplate` | Parse YAML; raises `KeyError`, `yaml.YAMLError` on problems |
| `validate_template` | `(template: PipelineTemplate) → list[str]` | Returns list of error strings (empty = valid) |
| `get_execution_order` | `(template: PipelineTemplate) → list[list[str]]` | Kahn's topological sort; returns waves of phase IDs |

---

### `PhaseSequencer`

Executes a pipeline template phase by phase, forwarding outputs downstream.

```python
from orchestration_engine.sequencer import PhaseSequencer

sequencer = PhaseSequencer(
    template=template,
    runner=runner,         # TaskRunner instance
    config={"tone": "professional"},
)

result = sequencer.execute({
    "brief": "Remote work trends in 2025",
    "target_audience": "HR leaders",
})

print(result["phase_outputs"])   # {phase_id: result_dict, ...}
print(result["final_output"])    # result dict of the last phase
```

If a phase fails, execution stops immediately and the return dict includes `"failed_phase"` and `"aborted": True`.

**Public methods:**

| Method | Signature | Description |
|---|---|---|
| `__init__` | `(template, runner, config=None)` | Binds template and runner; initialises `phase_outputs = {}` |
| `execute` | `(initial_input: dict) → dict` | Run all phases; returns `{phase_outputs, final_output}` |

**Template variable interpolation inside prompts:**

| Variable | Resolves to |
|---|---|
| `{input}` | The full `initial_input` dict |
| `{input[key]}` | A specific key from `initial_input` |
| `{previous_output}` | All accumulated phase outputs |
| `{previous_output[phase_id]}` | Output of a specific earlier phase |
| `{config}` | The pipeline-level config dict |

Missing keys produce `<MISSING:key>` placeholders rather than raising errors.

---

## Template YAML Schema

Pipeline templates are YAML files that define the full execution plan.

### Top-level fields

```yaml
id: my-pipeline                    # required; unique identifier
name: "My Pipeline"                # required; human-readable name
version: "1.0.0"                   # optional; default "1.0.0"
description: "What this does"      # optional

config_schema:                     # optional; documents expected inputs
  brief:
    type: string
    description: "Article topic"

phases:
  - ...                            # list of PhaseDefinition objects
```

### Phase fields

```yaml
phases:
  - id: research                   # required; unique within template
    name: "Research Phase"         # required; display name
    description: "..."             # optional
    task_type: research            # content | research | code | review | translation
    model_tier: sonnet             # haiku | sonnet | opus
    thinking_level: low            # off | low | medium | high
    depends_on:                    # list of phase IDs this phase waits for
      - []                         # empty = no dependencies (runs first)
    timeout_minutes: 30            # default 30
    prompt_template: |             # Python str.format()-style prompt
      Research the following topic thoroughly.
      Topic: {input[brief]}
      Target audience: {input[target_audience]}
      Return a JSON object with: sources, key_facts, statistics.
    output_schema:                 # optional; documents expected output shape
      sources:
        type: array
      key_facts:
        type: array
```

### Minimal working example

```yaml
id: summarise
name: "Summarise Pipeline"
phases:
  - id: summarise
    name: "Summarise"
    task_type: content
    model_tier: haiku
    thinking_level: off
    prompt_template: "Summarise the following in 3 sentences: {input[text]}"
```

### Multi-phase example with dependencies

```yaml
id: content-pipeline
name: "Content Pipeline"
version: "1.0.0"

phases:
  - id: research
    name: "Research"
    task_type: research
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: |
      Research: {input[brief]}
      Audience: {input[target_audience]}
      Output JSON: {{"sources": [...], "key_facts": [...]}}

  - id: write
    name: "Write Draft"
    task_type: content
    model_tier: sonnet
    thinking_level: low
    depends_on: [research]
    prompt_template: |
      Write an article based on this research:
      {previous_output[research]}

  - id: fact_check
    name: "Fact Check"
    task_type: review
    model_tier: sonnet
    thinking_level: medium
    depends_on: [write, research]
    prompt_template: |
      Article draft: {previous_output[write]}
      Source research: {previous_output[research]}
      List any factual inaccuracies. Score accuracy 0-100.
```

### Execution order rules

- Phases with no `depends_on` (or empty list) run in Wave 1
- A phase runs only after all its dependencies have succeeded
- If two phases have no dependency relationship, they are grouped into the same wave (currently executed sequentially; parallel execution is planned)
- Cycles are detected by `validate_template()` and reported as errors

---

## Model Identifiers

The engine uses a three-level naming system. This table maps each tier name to the exact model string sent to the API and to the Python enum constant used in code:

| Tier | Full Model String | Executor Reference |
|------|-------------------|--------------------|
| `haiku` | `anthropic/claude-haiku-4-5-20251001` | `ModelTier.HAIKU` |
| `sonnet` | `anthropic/claude-sonnet-4-6` | `ModelTier.SONNET` |
| `opus` | `anthropic/claude-opus-4-6` | `ModelTier.OPUS` |

**Where each name appears:**

- **Tier name** (`haiku`, `sonnet`, `opus`) — used in template YAML (`model_tier: sonnet`) and CLI flags (`--model sonnet`).
- **Full model string** — the exact identifier sent to the Anthropic Messages API or an OpenAI-compatible endpoint. Also used in OpenClaw configuration (`anthropic/claude-sonnet-4-6`).
- **Executor reference** — the `ModelTier` enum value used in Python code and returned in `TaskResult.model_used`.

**Example:** A phase with `model_tier: haiku` results in an API call using `claude-haiku-4-5-20251001` and a `TaskResult` where `model_used == "claude-haiku-4-5-20251001"` and `preferred_model == ModelTier.HAIKU`.

---

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | API key for `AnthropicExecutor`; required for live runs |

---

## Exit Codes

All CLI commands follow Unix conventions:

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Error (task not found, validation failed, API error, etc.) |
