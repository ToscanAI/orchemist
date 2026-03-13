# Pipeline Chaining

> **Module**: `orchestration_engine.chains` (422 lines), `orchestration_engine.chain_monitor` (280 lines)
> **Issue**: #330.1 (schema), #330.2 (engine), #330.3 (CLI/REST), #508 (monitoring)
> **Status**: Fully implemented — evaluation, spawning, monitoring, CLI, REST, recursive CTE queries

Pipeline chaining lets a completed run automatically launch one or more
downstream pipelines.  The parent template declares an `on_complete:` block
that maps final statuses (`success` / `failed`) to child template entries.
The daemon evaluates this block after the parent run reaches its terminal
status, inserts child `pipeline_runs` rows, and spawns independent daemon
subprocesses — each child running the full pipeline lifecycle
(sequencer → scoring → routing → chaining) in isolation.

---

## Table of Contents

1. [Template Configuration](#template-configuration)
2. [Data Model (OnCompleteConfig)](#data-model-oncompleteconfig)
3. [Placeholder Interpolation](#placeholder-interpolation)
4. [Depth Limiting](#depth-limiting)
5. [Evaluation Logic](#evaluation-logic)
6. [Child Run Spawning](#child-run-spawning)
7. [Daemon Integration](#daemon-integration)
8. [Database Schema](#database-schema)
9. [CLI Commands](#cli-commands)
10. [REST API](#rest-api)
11. [Chain Monitor](#chain-monitor)
12. [Real-World Example — Sprint Runner](#real-world-example--sprint-runner)
13. [Design Decisions](#design-decisions)

---

## Template Configuration

Add an `on_complete:` block at the template top level:

```yaml
on_complete:
  max_chain_depth: 12        # optional, default 5, hard cap 20
  success:
    - template: notify-pipeline
      input_map:
        parent_run: "{{run_id}}"
        summary: "{{final_output.summary}}"
        output_dir: "{{output_dir}}"
    - template: deploy-pipeline
      input_map:
        artifact_path: "{{final_output.artifact_path}}"
  failed:
    - template: incident-pipeline
      input_map:
        failed_run: "{{run_id}}"
        error_context: "{{status}}"
```

### Field Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `on_complete.success` | list | `[]` | Entries to launch when the parent succeeds |
| `on_complete.failed` | list | `[]` | Entries to launch when the parent fails |
| `on_complete.max_chain_depth` | int | `5` | Maximum chain depth (clamped to `[1, 20]`) |
| `entry.template` | string | *required* | Template name or file path |
| `entry.input_map` | dict | `{}` | Key→value mapping with `{{placeholder}}` tokens |

When `on_complete:` is absent, chaining is completely disabled (the daemon
skips the evaluation call). Setting `success: []` and `failed: []`
explicitly has the same effect.

---

## Data Model (OnCompleteConfig)

**Module**: `orchestration_engine.templates`

```
@dataclass
class OnCompleteEntry:
    template:  str                  # required — template name or path
    input_map: Dict[str, Any]      # default: {}

@dataclass
class OnCompleteConfig:
    success:         List[OnCompleteEntry]   # default: []
    failed:          List[OnCompleteEntry]   # default: []
    max_chain_depth: int                     # default: 5, min: 1
```

**Validation** (`__post_init__`):

- `OnCompleteEntry.template` must be a non-empty string — raises `ValueError`
  otherwise.
- `OnCompleteEntry.input_map` must be a dict — raises `TypeError` otherwise.
  `None` is coerced to `{}`.
- `OnCompleteConfig.max_chain_depth` is clamped to `max(1, int(value))`.

**Parser**: `_parse_on_complete_config(raw)` in `templates.py`:

- Returns `None` for non-dict input (feature disabled).
- Warns on unknown fields but does not fail.
- Each list entry must contain a `template` key; a missing key raises `ValueError`.

---

## Placeholder Interpolation

**Function**: `interpolate_input_map(input_map, context)`
**Regex**: `\{\{(\w+(?:\.\w+)*)\}\}` — matches `{{key}}` and `{{dotted.path}}`

### Context Keys

The interpolation context is built from the parent run record and the
pipeline result:

| Token | Source |
|-------|--------|
| `{{run_id}}` | Parent run's `run_id` |
| `{{output_dir}}` | Parent run's `output_dir` |
| `{{status}}` | Final status string (`success`, `failed`, …) |
| `{{final_output.key}}` | Nested lookup in result `final_output` dict |
| `{{any_input_key}}` | Top-level key from parent's `input_json` |

### Resolution Rules

1. **Dotted paths** — `_resolve_dotted("final_output.summary", context)` walks
   segment by segment. Returns `None` if any segment is missing or intermediate
   is not a dict.
2. **Unresolved tokens** — left verbatim (`{{unknown}}` → `"{{unknown}}"`),
   never silently replaced with empty string.
3. **Non-string values** — integers, booleans, lists, dicts are passed through
   unchanged (no placeholder scanning).

### Example

```python
context = {
    "run_id": "abc-123",
    "output_dir": "/tmp/out",
    "status":  "success",
    "final_output": {"summary": "All tests passed", "artifact_path": "/build/app.tar.gz"},
    "repo_url": "https://github.com/org/repo",
}

input_map = {
    "parent_run": "{{run_id}}",
    "summary": "{{final_output.summary}}",
    "repo": "{{repo_url}}",
    "unknown_field": "{{nonexistent}}",
    "count": 42,
}

resolved = interpolate_input_map(input_map, context)
# {
#     "parent_run": "abc-123",
#     "summary": "All tests passed",
#     "repo": "https://github.com/org/repo",
#     "unknown_field": "{{nonexistent}}",   ← verbatim
#     "count": 42,                           ← pass-through
# }
```

---

## Depth Limiting

Two independent limits prevent infinite chain loops:

| Limit | Controlled By | Value |
|-------|---------------|-------|
| **Template limit** | `on_complete.max_chain_depth` | Default `5`, configurable per template |
| **Hard cap** | `chains.MAX_ALLOWED_CHAIN_DEPTH` | `20` — cannot be overridden |

The effective limit is `min(template_limit, 20)`.

**Enforcement** in `evaluate_on_complete()`:

```python
parent_depth = int(run.get("chain_depth") or 0)
max_depth    = min(int(on_complete.max_chain_depth), MAX_ALLOWED_CHAIN_DEPTH)

if parent_depth >= max_depth:
    logger.warning("Chain depth limit reached …")
    return []     # no children spawned
```

Each child's `chain_depth` is `parent_depth + 1`, persisted in the
`pipeline_runs` row and available for the child's own chaining evaluation.

---

## Evaluation Logic

**Function**: `evaluate_on_complete(template, run, result, final_status)`

Sequence:

1. **Guard** — return `[]` if `template.on_complete` is `None`.
2. **Select entry list** — `success` entries when `final_status == "success"`,
   `failed` entries for everything else (including `scoring_failed`,
   `budget_exceeded`, etc.).
3. **Depth check** — return `[]` if `parent_depth >= max_depth`.
4. **Build context** — merge `run_id`, `output_dir`, `status`,
   `final_output`, and all parent input keys.
5. **Per-entry** — `interpolate_input_map()` on each entry's `input_map`.
6. **Return** — list of child config dicts, each containing:

```python
{
    "template_name":  "notify-pipeline",
    "input_map":      {"parent_run": "abc-123", ...},
    "chain_depth":    2,
    "parent_run_id":  "abc-123",
    "mode":           "standalone",       # inherited from parent
    "gateway_url":    "https://...",      # inherited from parent
    "skip_scoring":   False,              # inherited from parent
}
```

---

## Child Run Spawning

**Function**: `spawn_chain_runs(child_configs, db, db_path, parent_run_id)`

For each child config:

1. **Resolve template** — `_resolve_template_path(engine, template_name)`:
   - Tries the name as a file path first (if it has `.yaml`/`.yml` suffix
     or contains `/`).
   - Falls back to `TemplateEngine.resolve_template()` (name-based lookup
     through `templates/` + community search paths).

2. **Load template** — `engine.load_template(path)` to extract `template_id`.

3. **Create output directory** — `/tmp/orch-chains/<parent_8chars>/<child_8chars>`.

4. **Insert DB row** — `db.insert_pipeline_run()` with:
   - `parent_run_id` = parent's UUID
   - `chain_depth` = incremented depth
   - `status` = `"pending"`
   - `input_json` = serialised resolved `input_map`

5. **Spawn daemon** — `_spawn_daemon(run_id, db_path)`:
   ```
   python -m orchestration_engine.daemon <run_id> <db_path>
   ```
   - `subprocess.Popen` with `start_new_session=True` (fully detached).
   - `stdout` / `stderr` redirected to `DEVNULL` (daemon writes its own log).
   - Parent environment inherited (so API keys are available).

6. **Error handling** — per-child failures are logged and skipped.  If the
   daemon spawn fails, the child row is marked `"failed"` with
   `error_message` = `"Daemon spawn failed: …"`.

**Returns**: list of successfully spawned child run IDs.

---

## Daemon Integration

Chain evaluation runs at the very end of `daemon.run_daemon()`, after all
other post-pipeline work completes:

```
Pipeline execute()
  → Scoring (auto-score)
    → Postflight (Definition of Done)
      → Routing dispatch (confidence → action tier)
        → DB status update (success/failed/scoring_failed/…)
          → GitHub result hook (create/update PR)
            → Deferred auto-merge
              → ★ Chain execution (evaluate + spawn)
                → PID file cleanup + daemon exit
```

The chain block is wrapped in a `try/except` — failures are non-fatal
because the parent run has already been persisted with its terminal status.

```python
# daemon.py — chain execution block (Issue #330.2)
try:
    from .chains import evaluate_on_complete, spawn_chain_runs
    child_configs = evaluate_on_complete(
        template=template, run=run,
        result=result or {}, final_status=_final_status,
    )
    if child_configs:
        spawned = spawn_chain_runs(
            child_configs=child_configs, db=db,
            db_path=db_path, parent_run_id=run_id,
        )
        logger.info("Chain execution: spawned %d child run(s): %s",
                     len(spawned), spawned)
except Exception as exc:
    logger.warning("Chain execution failed (non-fatal): %s", exc)
```

---

## Database Schema

### Columns (migration 006)

Added to `pipeline_runs` by `_migration_006_add_chain_columns`:

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `parent_run_id` | `TEXT` | `NULL` | FK to parent run (NULL = root / standalone) |
| `chain_depth` | `INTEGER` | `0` | Nesting depth (0 = root) |

### Index (migration 019)

```sql
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_parent_run_id
ON pipeline_runs(parent_run_id)
```

Enables fast child-lookup and chain traversal queries — Issue #508.

### Query Methods

| Method | Description |
|--------|-------------|
| `list_pipeline_run_children(parent_run_id)` | Return direct children ordered by `created_at ASC` |
| `get_full_chain(root_run_id)` | Recursive CTE walk returning all descendants (root first) |
| `list_active_chain_roots(limit)` | Find root runs with at least one non-terminal descendant |

#### Recursive CTE — `get_full_chain`

```sql
WITH RECURSIVE chain(run_id, depth) AS (
    SELECT run_id, 0 FROM pipeline_runs WHERE run_id = ?
    UNION ALL
    SELECT pr.run_id, chain.depth + 1
    FROM pipeline_runs pr
    JOIN chain ON pr.parent_run_id = chain.run_id
    WHERE chain.depth < 50
)
SELECT pr.*
FROM pipeline_runs pr
JOIN chain ON pr.run_id = chain.run_id
ORDER BY chain.depth ASC, pr.created_at ASC
```

#### Active Chains — `list_active_chain_roots`

Uses a recursive CTE to find all root runs (`parent_run_id IS NULL`) that
have at least one descendant whose `status NOT IN (terminal_statuses)`.
Ordered by `created_at DESC`.

---

## CLI Commands

### `orch children <RUN_ID>`

List direct child runs spawned by a parent.

```
$ orch children a3f8c2d1

RUN ID                                TEMPLATE                        DEPTH  STATUS
------------------------------------------------------------------------------------------
b7e4f190-...-4c21                     notify-pipeline                     1  success
c8d5a201-...-7f33                     deploy-pipeline                     1  running
```

Options:
- `--db-path PATH` — override the persistent DB path.

### `orch chain <RUN_ID>`

Display the full chain tree for any run (walks up to root, then shows all
descendants).

```
$ orch chain c8d5a201

(Showing chain from root: a3f8c2d1)
RUN ID      ISSUE     STATUS          SCORE   ELAPSED  TEMPLATE
────────────────────────────────────────────────────────────────────────────────
a3f8c2d1    #42       success         0.950       45s  sprint-runner-v1
  b7e4f190  #42       success         0.920      1m 2s  sprint-runner-step-v1
    c8d5a20  #43       running           —         30s  sprint-runner-v1
```

### `orch chain --active`

List all currently active (non-terminal) chains.

```
$ orch chain --active

ROOT RUN    TEMPLATE                        STATUS           DEPTH  CREATED
────────────────────────────────────────────────────────────────────────────────
a3f8c2d1    sprint-runner-v1                success              0  2025-01-15T10:30
```

Options:
- `--db-path PATH` — override the persistent DB path.

**Mutual exclusion**: provide either `RUN_ID` or `--active`, not both.

---

## REST API

### `GET /api/v1/runs/{run_id}/children`

Returns all direct child runs for a given parent.

**Response** (200):

```json
{
  "run_id": "a3f8c2d1-...",
  "children": [
    {
      "run_id": "b7e4f190-...",
      "template_id": "notify-pipeline",
      "chain_depth": 1,
      "status": "success",
      "parent_run_id": "a3f8c2d1-...",
      ...
    }
  ]
}
```

**Errors**: `404` when parent run ID is not found.

---

## Chain Monitor

**Module**: `orchestration_engine.chain_monitor` (Issue #508)

Provides chain traversal and display utilities used by the CLI:

### Functions

| Function | Description |
|----------|-------------|
| `find_chain_root(db, run_id)` | Walk `parent_run_id` links upward to find the root (bounded by `MAX_ALLOWED_CHAIN_DEPTH`) |
| `get_issue_for_run(db, run_id)` | Look up linked GitHub issue number via `issue_pipeline_map` |
| `compute_elapsed(run)` | Calculate wall-clock seconds from `started_at` to `completed_at` (or now) |
| `format_chain_row(run, issue_number, depth)` | Format one run as a fixed-width display row with depth indentation |
| `build_chain_display(db, root_run_id)` | Build full chain tree string (header + all descendants) |
| `build_active_chains_display(db)` | Build display of all active chain roots |

### Display Format

Each row is formatted with fixed-width columns:

```
<indent><run_id[:8]>  #<issue|—>  <status:14>  <score:6>  <elapsed:8>  <template[:24]>
```

Depth drives 2-space-per-level indentation. Elapsed time is formatted as
`45s`, `1m 30s`, or `1h 1m 1s`.

---

## Real-World Example — Sprint Runner

The sprint runner templates demonstrate **ping-pong chaining**: two templates
call each other to process issues one at a time.

### Flow

```
sprint-runner-v1 (picks next issue from queue)
  → on success → sprint-runner-step-v1 (executes the issue)
      → on success → sprint-runner-v1 (picks next issue)
          → on success → sprint-runner-step-v1
              → ... (repeats until queue is empty or depth limit reached)
```

### sprint-runner-v1.yaml

```yaml
on_complete:
  max_chain_depth: 12
  success:
    - template: sprint-runner-step-v1
      input_map:
        sprint_name: "{{sprint_name}}"
        repo_path: "{{repo_path}}"
        repo_url: "{{repo_url}}"
        parent_output_dir: "{{output_dir}}"
        test_command: "{{test_command}}"
        language: "{{language}}"
        style_guide: "{{style_guide}}"
        files_context: "{{files_context}}"
        sprint_queue_config: "{{sprint_queue_config}}"
  failed: []
```

### sprint-runner-step-v1.yaml

```yaml
on_complete:
  max_chain_depth: 12
  success:
    - template: sprint-runner-v1
      input_map:
        sprint_name: "{{sprint_name}}"
        repo_path: "{{repo_path}}"
        repo_url: "{{repo_url}}"
        parent_output_dir: "{{output_dir}}"
        issues_json: "[]"
        test_command: "{{test_command}}"
        language: "{{language}}"
        style_guide: "{{style_guide}}"
        files_context: "{{files_context}}"
  failed: []
```

### Key Observations

- **`max_chain_depth: 12`** — allows up to 12 hops (6 full issue cycles).
- **`failed: []`** — chain stops on failure to prevent cascading broken merges.
- **Input forwarding** — all parent inputs are forwarded via placeholders so
  each child has the same repository/queue context.
- **`issues_json: "[]"`** — step-v1 passes an empty issues list back to
  runner-v1, which re-reads the queue file to pick the next issue.

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Daemon-per-child** | Full process isolation — each child has its own PID file, log, SIGTERM handler, cost tracker. A child failure cannot corrupt the parent. |
| **Detached subprocess** (`start_new_session=True`) | Child survives even if the parent daemon exits immediately after spawning. |
| **Non-fatal chain errors** | Parent run is already persisted with its terminal status before chain evaluation. Chain failures should not retroactively change the parent's status. |
| **Unresolved placeholders left verbatim** | Downstream pipelines can see what was unresolved and handle it, vs. silently receiving empty strings. |
| **Output in `/tmp/orch-chains/`** | Co-located under parent's namespace but isolated per child. Uses 8-char UUID prefixes for directory names. |
| **Mode inheritance** | Children inherit `mode`, `gateway_url`, and `skip_scoring` from the parent so API keys and runtime config are consistent. |
| **Hard cap of 20** | `MAX_ALLOWED_CHAIN_DEPTH` exists independently of the template setting to defend against misconfiguration (e.g. `max_chain_depth: 999`). |
| **Recursive CTE for traversal** | SQLite supports `WITH RECURSIVE` — single query fetches entire chain tree with no N+1 problem. Bounded at depth 50 as safety. |

---

*Source files*: `src/orchestration_engine/chains.py`,
`src/orchestration_engine/chain_monitor.py`,
`src/orchestration_engine/templates.py` (OnCompleteConfig),
`src/orchestration_engine/daemon.py` (integration block),
`src/orchestration_engine/db.py` (schema + queries),
`src/orchestration_engine/cli.py` (`orch children`, `orch chain`),
`src/orchestration_engine/web/api.py` (`GET /runs/{id}/children`)
