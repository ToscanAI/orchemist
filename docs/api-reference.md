# API Reference

> **Audience:** Developers integrating with or extending the orchestration engine. Covers the CLI, core Python classes, and template YAML schema.

---

## CLI Commands

The `orch` entry point is installed by `pip install orchemist`. All commands accept `--db-path <path>` (custom database location) and `-v`/`--verbose` (debug logging) as global options.

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

Phases in the same wave are independent and execute in parallel by default. Use the `parallel` field on a phase or `max_parallel` at the pipeline level to control concurrency.

---

### `orch launch` — Run a pipeline in the background

Spawns a daemon process that runs the pipeline, then exits immediately. Use `orch status`, `orch logs`, and `orch wait` to monitor progress.

```bash
orch launch content-pipeline --mode openclaw --input '{"brief": "AI trends"}'
orch launch coding-pipeline-v1 --issue 42 --repo owner/repo
orch launch my-template.yaml --input-file params.json --output-dir ./results
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `TEMPLATE_NAME_OR_FILE` | argument | required | Template name, ID, or path to a YAML file |
| `--mode` | choice | `standalone` | `standalone`, `openclaw`, or `dry-run` |
| `--input` | JSON string | none | Pipeline input as a JSON string |
| `--input-file` | path | none | Path to a JSON file containing pipeline input |
| `--issue` | int | none | GitHub issue number to auto-fetch as pipeline input |
| `--repo` | string | `$GITHUB_REPOSITORY` | Repository slug (`owner/repo`) for `--issue` |
| `--output-dir` | path | auto-generated | Directory for phase outputs |
| `--gateway-url` | string | `$OPENCLAW_GATEWAY_URL` | OpenClaw gateway URL (openclaw mode) |
| `--skip-scoring` | flag | false | Skip auto-scoring even if the template declares a scenario |
| `--db-path` | path | `~/.orchestration-engine/engine.db` | Override persistent DB path |

**Output:**
```
✓ Pipeline launched in background
  Run ID:  a3f8c2d1
  PID:     12345
  Output:  ./output/content-pipeline-20260313-143022-a3f8c2d1/

  Status:  orch status a3f8c2d1
  Logs:    orch logs a3f8c2d1
  Wait:    orch wait a3f8c2d1
```

---

### `orch wait` — Block until a pipeline finishes

Polls the DB until the run reaches a terminal state (success, failed, cancelled, crashed, scoring_failed, pending_review, rejected). Exits 0 on success, 2 on failure or timeout.

```bash
orch wait a3f8c2d1
orch wait a3f8c2d1 --timeout 120
orch wait a3f8c2d1 --interval 5
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `RUN_ID` | argument | required | Pipeline run ID |
| `--timeout` | int | `1800` | Maximum seconds to wait |
| `--interval` | int | `3` | Poll interval in seconds |
| `--db-path` | path | auto | Override persistent DB path |

---

### `orch logs` — Show daemon logs

Prints the daemon log file for a pipeline run. Use `--follow` to tail in real time.

```bash
orch logs a3f8c2d1
orch logs a3f8c2d1 --follow
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `RUN_ID` | argument | required | Pipeline run ID |
| `--follow` / `-f` | flag | false | Tail the log file (like `tail -f`) |
| `--db-path` | path | auto | Override persistent DB path |

---

### `orch children` — List child pipeline runs

Shows all child pipeline runs spawned by a parent run via `on_complete` chaining.

```bash
orch children a3f8c2d1
```

**Output columns:** RUN ID, TEMPLATE, DEPTH, STATUS

---

### `orch chain` — Monitor pipeline chain execution

Displays the full chain tree for a given run (tracing back to the root), or lists all currently active chains.

```bash
orch chain a3f8c2d1          # Show full chain for a run
orch chain --active           # List all running chains
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `RUN_ID` | argument | optional | Run ID to inspect (traces to root) |
| `--active` | flag | false | List all currently active (non-terminal) chains |
| `--db-path` | path | auto | Override persistent DB path |

---

### `orch resume` — Resume a failed pipeline *(stub)*

> **Note:** This command is not yet implemented. It returns an error message and exits with code 1. Re-run from scratch with `orch launch` instead.

```bash
orch resume a3f8c2d1
```

---

### `orch gate` — Manage merge gates

After a git-enabled pipeline completes, it creates a merge gate requiring human approval before the feature branch is merged. The `gate` group provides subcommands to list, inspect, approve, and reject gates.

```bash
orch gate list                      # Show pending gates
orch gate list --all                # Include approved/rejected
orch gate info abc12345             # Show gate details
orch gate approve abc12345          # Approve a gate
orch gate approve abc12345 --force  # Override a failed score gate
orch gate reject abc12345 -m "Needs rework"
```

#### `orch gate list`

| Option | Description |
|---|---|
| `--all` | Show all gates including approved and rejected (default: pending only) |

#### `orch gate info <RUN_ID>`

Displays: status (with emoji), pipeline ID, branch, base branch, diff stats, scoring status and score, creation/update timestamps, approval/rejection message, and commit list.

#### `orch gate approve <RUN_ID>`

Approves a pending gate. If the pipeline's scoring status is `failed`, approval is blocked unless `--force` is specified.

| Option | Description |
|---|---|
| `-m` / `--message` | Optional approval message |
| `-f` / `--force` | Override score gate enforcement |

If the template has `create_pr: true`, a GitHub PR is created automatically on approval.

#### `orch gate reject <RUN_ID>`

Rejects a gate. The feature branch is preserved for inspection.

| Option | Description |
|---|---|
| `-m` / `--message` | Optional rejection reason |

---

### `orch new` — Scaffold a new pipeline template

Interactive wizard that walks you through naming a pipeline, adding phases, choosing model tiers and thinking levels, and wiring up dependencies. Generates a valid YAML file.

```bash
orch new                              # Interactive wizard
orch new --yes                        # Non-interactive with sensible defaults
orch new --from hello-pipeline        # Clone an existing template
orch new --yes --phases 4 --output ./my-templates/pipeline.yaml
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `--yes` / `-y` | flag | false | Non-interactive mode with sensible defaults |
| `--from` | string | none | Clone an existing template as starting point |
| `--output` | path | `./templates/<name>.yaml` | Output file path |
| `--force` / `-f` | flag | false | Overwrite output if it exists |
| `--phases` | int | 2 | Number of phases (primarily for `--yes` mode) |

---

### `orch import plugin-command` — Convert a plugin command to a template

Converts a knowledge-work-plugin Markdown command file (with optional YAML frontmatter) into a PipelineTemplate YAML file.

```bash
orch import plugin-command campaign-plan.md
orch import plugin-command draft-content.md --output my-draft.yaml
orch import plugin-command brand-review.md --dry-run
orch import plugin-command campaign-plan.md --validate
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `COMMAND_FILE` | argument | required | Path to the Markdown plugin command file |
| `-o` / `--output` | path | `<command-id>.yaml` | Output YAML path |
| `--author` | string | auto | Author string for the generated template |
| `--dry-run` | flag | false | Print generated YAML to stdout without writing |
| `--validate` | flag | false | Run `orch validate` on the generated file after writing |

---

### `orch serve` — Launch the web UI

Starts a FastAPI server that serves the browser-based web UI (dashboard, template cards, run detail with live SSE progress). Requires the `[web]` extra.

```bash
pip install orchemist[web]

orch serve                    # http://127.0.0.1:8374
orch serve --port 9000
orch serve --no-open          # Don't auto-open browser
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `--port` | int | `8374` | Port to serve on |
| `--host` | string | `127.0.0.1` | Host to bind to |
| `--no-open` | flag | false | Skip auto-opening the browser |

---

### `orch ui` — Serve the static Next.js frontend

Serves the pre-built Next.js frontend export from `frontend/out/` using Python's built-in HTTP server. Build the frontend first with `cd frontend && npm run build`.

```bash
orch ui                       # http://localhost:8080
orch ui --port 9090
orch ui --host 0.0.0.0       # Bind to all interfaces
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `--port` | int | `8080` | Port to serve on |
| `--host` | string | `127.0.0.1` | Host to bind to |
| `--no-open` | flag | false | Skip auto-opening the browser |

---

### `orch api-server` — Launch the REST API server

Starts the versioned REST API at `/api/v1/` (backed by the same daemon infrastructure as `orch launch`). Intended for CI/CD pipelines, webhooks, and programmatic consumers. Requires the `[web]` extra.

```bash
pip install orchemist[web]

orch api-server                    # http://127.0.0.1:8375/api/v1/
orch api-server --port 9000
orch api-server --reload           # Dev mode with auto-reload
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `--port` | int | `8375` | Port to serve on |
| `--host` | string | `127.0.0.1` | Host to bind to |
| `--reload` | flag | false | Enable auto-reload (development only) |
| `--db-path` | path | auto | Override persistent DB path |

Visit `http://host:port/api/v1/docs` for the interactive Swagger UI (auto-generated by FastAPI).

---

### `orch rubric generate` — Generate an LLM Judge rubric

Extracts evaluation criteria from a skill Markdown file and generates a rubric YAML file suitable for use with `LLMJudgeGrader` in scenario tests.

```bash
orch rubric generate path/to/SKILL.md
orch rubric generate path/to/SKILL.md --output my-rubric.yaml
orch rubric generate path/to/SKILL.md -o results/rubric.yaml --force
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `SKILL_FILE` | argument | required | Path to a skill Markdown file |
| `-o` / `--output` | path | `<skill-name>-rubric.yaml` | Output YAML file path |
| `-f` / `--force` | flag | false | Overwrite output if it exists |

**Generated YAML contains:** `rubric` (text for LLMJudgeGrader), `criteria` (machine-readable checklist), `name`, `generated_from`, `generated_at`.

---

### `orch scenario run` — Run an E2E scenario test

Executes a pipeline template referenced by a scenario YAML file, grades the output against acceptance criteria, and prints a score report. Used for automated quality assurance.

```bash
# Dry-run (no API key needed):
ORCH_DRY_RUN=1 orch scenario run e2e-autonomous --dry-run

# Live run:
orch scenario run e2e-autonomous --api-key sk-ant-...

# Custom scenario directory:
orch scenario run my-scenario --scenario-dir tests/scenarios/ --dry-run
```

**Options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `SCENARIO_ID` | argument | required | Scenario YAML stem or file path |
| `--dry-run` | flag | false | No real API calls; graders return stub scores |
| `--scenario-dir` | path | `./scenarios/` | Directory to search for scenario files |
| `--api-key` | string | `$ANTHROPIC_API_KEY` | API key for live mode |
| `--mode` | choice | `standalone` | `standalone` (direct API) or `openclaw` (gateway) |
| `--gateway-url` | string | `$OPENCLAW_GATEWAY_URL` | Gateway URL for openclaw mode |
| `--gateway-token` | string | `$OPENCLAW_GATEWAY_TOKEN` | Gateway token for openclaw mode |

**Exit codes:** 0 if the scenario passes (score ≥ threshold), 1 otherwise.

**Output:** A rich table showing per-criterion breakdown (criterion ID, grader type, weight, score, PASS/FAIL) followed by an overall score summary with gate status.

---

### `orch reviews` — Manage the human review queue

When a pipeline run finishes with a confidence score in the human-review tier, it enters a `pending_review` status. The `reviews` group provides subcommands to list, approve, and reject these runs.

```bash
orch reviews list                         # Show pending reviews
orch reviews list --limit 50 --offset 20
orch reviews approve abc12345
orch reviews approve abc12345 --reviewed-by "conny" --note "Looks good"
orch reviews reject abc12345 "Quality too low for publication"
```

#### `orch reviews list`

| Option | Type | Default | Description |
|---|---|---|---|
| `--limit` | int | `20` | Maximum number of items |
| `--offset` | int | `0` | Number of items to skip |
| `--db-path` | path | auto | Override persistent DB path |

**Output columns:** RUN ID, TEMPLATE, CREATED AT, SCORE, TIER

#### `orch reviews approve <RUN_ID>`

Moves the run from `pending_review` to `success` status.

| Option | Description |
|---|---|
| `--reviewed-by` | Reviewer identifier (for audit trail) |
| `--note` | Optional review note |
| `--db-path` | Override persistent DB path |

#### `orch reviews reject <RUN_ID> <REASON>`

Moves the run from `pending_review` to `rejected` status.

| Option | Description |
|---|---|
| `--reviewed-by` | Reviewer identifier (for audit trail) |
| `--db-path` | Override persistent DB path |

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
