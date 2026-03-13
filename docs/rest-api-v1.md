# REST API v1 Reference

> **Base URL:** `http://localhost:8375/api/v1/`
> **Start with:** `orch api-server` (requires `pip install orchemist[web]`)
> **Interactive docs:** `http://localhost:8375/api/v1/docs` (Swagger UI)
> **OpenAPI spec:** `http://localhost:8375/api/v1/openapi.json`

The versioned REST API is separate from the browser UI API (`/api/`). It is designed for programmatic consumers: CI/CD pipelines, webhooks, external scripts, and the OpenClaw gateway.

**Authentication:** None by default (local use). Webhook endpoints support HMAC-SHA256 signature verification when a `secret` is configured.

**CORS:** Wide-open by default (`*`). Tighten `allow_origins` in production deployments.

---

## Table of Contents

1. [Health](#1-health)
2. [Templates](#2-templates)
3. [Pipeline Runs](#3-pipeline-runs)
4. [Webhooks & Triggers](#4-webhooks--triggers)
5. [Human Reviews](#5-human-reviews)
6. [Cost Tracking](#6-cost-tracking)
7. [Trust Profiles](#7-trust-profiles)
8. [Integrations](#8-integrations)

---

## 1. Health

### `GET /api/v1/health`

Basic health check.

**Response** `200`:
```json
{
  "status": "ok",
  "version": "0.9.0"
}
```

---

### `GET /api/v1/health/webhook`

Health check for the regression CI webhook trigger. Verifies DB trigger registration and (best-effort) GitHub webhook existence.

**Response** `200`:
```json
{
  "trigger_registered": true,
  "trigger_id": "regression-ci-trigger",
  "github_webhook_id": 123456,
  "github_webhook_active": true,
  "status": "ok"
}
```

| Field | Type | Description |
|---|---|---|
| `trigger_registered` | bool | Whether the trigger row exists in the DB |
| `trigger_id` | string \| null | The trigger ID checked |
| `github_webhook_id` | int \| null | GitHub webhook ID (null if check skipped) |
| `github_webhook_active` | bool \| null | Whether the GitHub hook is active |
| `status` | string | `"ok"`, `"degraded"` (trigger exists but GitHub check failed), or `"error"` (trigger missing) |

---

## 2. Templates

### `GET /api/v1/templates`

List all discoverable pipeline templates.

**Response** `200`:
```json
[
  {
    "id": "content-pipeline-v27",
    "name": "Content Pipeline v2.7",
    "version": "2.7.0",
    "phases_count": 7,
    "description": "7-phase content pipeline with fact-checking.",
    "source": "bundled",
    "phases": [
      {
        "id": "research",
        "name": "Research",
        "model_tier": "sonnet",
        "thinking_level": "low",
        "depends_on": []
      }
    ],
    "config_schema": { "type": "object", "properties": { "topic": { "type": "string" } } }
  }
]
```

---

### `GET /api/v1/templates/{name}`

Get detail for a single template by name or ID.

**Path parameters:**
| Parameter | Description |
|---|---|
| `name` | Template file stem or `id` field |

**Response** `200`:
```json
{
  "id": "content-pipeline-v27",
  "name": "Content Pipeline v2.7",
  "version": "2.7.0",
  "description": "...",
  "author": "ToscanAI",
  "tags": ["content", "article"],
  "phases": [
    {
      "id": "research",
      "name": "Research",
      "description": "...",
      "model_tier": "sonnet",
      "thinking_level": "low",
      "depends_on": [],
      "task_type": "research"
    }
  ],
  "example_input": { "topic": "AI safety" },
  "config_schema": {}
}
```

**Errors:** `404` — template not found.

---

### `POST /api/v1/templates/validate`

Validate template YAML without writing to disk.

**Request body:**
```json
{
  "content": "id: test\nname: Test\nphases:\n  - id: p1\n    name: P1\n    task_type: content",
  "extended": true
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `content` | string | required | Raw YAML content |
| `extended` | bool | `true` | Also run extended linting |

**Response** `200`:
```json
{
  "valid": true,
  "errors": [],
  "warnings": ["Recommended field 'author' missing"]
}
```

**Errors:** `422` — YAML parse error or structurally invalid.

---

### `POST /api/v1/templates`

Create a new pipeline template.

**Request body:**
```json
{
  "content": "id: my-pipeline\nname: My Pipeline\nphases: ...",
  "source": "user",
  "overwrite": false
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `content` | string | required | Raw YAML content |
| `source` | string | `"user"` | `"user"` (→ `~/.orch/templates/`) or `"project"` (→ `./templates/`) |
| `overwrite` | bool | `false` | Allow replacing an existing template |

**Response** `201`:
```json
{
  "id": "my-pipeline",
  "name": "My Pipeline",
  "version": "1.0.0",
  "path": "/home/user/.orch/templates/my-pipeline.yaml",
  "source": "user",
  "phases_count": 3,
  "created": true,
  "warnings": []
}
```

**Errors:** `409` — template exists and `overwrite` is false. `422` — validation failed.

---

### `PUT /api/v1/templates/{name}`

Update an existing user-owned template.

**Path parameters:** `name` — template name or ID.

**Request body:** Same as `POST /api/v1/templates`.

**Response** `200`: Same shape as create, with `"created": false`.

**Errors:** `403` — template is bundled or project-owned (read-only). `404` — not found. `422` — validation failed.

---

### `DELETE /api/v1/templates/{name}`

Delete a user-owned template.

**Response** `204`: No content.

**Errors:** `403` — template is not user-owned. `404` — not found.

---

## 3. Pipeline Runs

### `POST /api/v1/runs`

Launch a new pipeline run in the background (equivalent to `orch launch`).

**Request body:**
```json
{
  "template": "content-pipeline-v27",
  "mode": "standalone",
  "input": {
    "topic": "AI safety trends",
    "tone": "professional"
  },
  "output_dir": null,
  "gateway_url": null,
  "skip_scoring": false
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `template` | string | required | Template name, ID, or path to `.yaml` file |
| `mode` | string | `"dry-run"` | `"standalone"`, `"openclaw"`, or `"dry-run"` |
| `input` | dict | `{}` | Pipeline input variables |
| `output_dir` | string | auto | Output directory (auto-generated if null) |
| `gateway_url` | string | `$OPENCLAW_GATEWAY_URL` | OpenClaw gateway URL |
| `skip_scoring` | bool | `false` | Skip auto-scoring |

**Response** `201`: [RunResponse](#runresponse-schema)

**Errors:** `400` — invalid template. `404` — template not found. `422` — validation errors.

---

### `GET /api/v1/runs`

List pipeline runs with optional filtering.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `status` | string | null | Filter: `pending`, `running`, `success`, `failed`, `cancelled`, `crashed` |
| `template_id` | string | null | Filter by template ID |
| `limit` | int | `20` | Page size (max 100) |
| `offset` | int | `0` | Pagination offset |

**Response** `200`:
```json
{
  "items": [ /* RunResponse objects */ ],
  "total": 42,
  "limit": 20,
  "offset": 0
}
```

---

### `GET /api/v1/runs/{run_id}`

Get the current state of a pipeline run. Includes a daemon PID liveness check — if the process died but the status is still `running`, it is auto-corrected to `crashed`.

**Response** `200`: [RunResponse](#runresponse-schema)

**Errors:** `404` — run not found.

---

### `GET /api/v1/runs/{run_id}/children`

List child pipeline runs spawned by a parent run via `on_complete` chaining.

**Response** `200`:
```json
{
  "run_id": "a3f8c2d1",
  "children": [ /* RunResponse objects */ ]
}
```

**Errors:** `404` — parent run not found.

---

### `GET /api/v1/runs/{run_id}/logs`

Get the daemon log file contents.

**Response** `200`:
```json
{
  "run_id": "a3f8c2d1",
  "log": "2026-03-13 14:30:22 INFO Starting phase research..."
}
```

**Errors:** `404` — run not found or log file missing.

---

### `GET /api/v1/runs/{run_id}/stream`

**Server-Sent Events (SSE)** stream of live phase-transition events.

Connect with an `EventSource` client. Events are emitted as the daemon writes them to the DB, polled every 1 second. The stream closes automatically when the run reaches a terminal state.

**Event types:**

| Event | Description | Data fields |
|---|---|---|
| `phase_started` | Phase execution began | `run_id`, `phase_id`, `created_at` |
| `phase_completed` | Phase finished | `run_id`, `phase_id`, `tokens_consumed`, `cost_usd`, `state`, `created_at` |
| `status_changed` | Run reached terminal state | `run_id`, `status`, `completed_at`, `error_message` |
| `error` | Run not found | `error` |

**Example:**
```
event: phase_started
data: {"run_id": "a3f8c2d1", "phase_id": "research", "created_at": "2026-03-13T14:30:22"}

event: phase_completed
data: {"run_id": "a3f8c2d1", "phase_id": "research", "tokens_consumed": 2341, "cost_usd": 0.004, "state": "success"}

event: status_changed
data: {"run_id": "a3f8c2d1", "status": "success", "completed_at": "2026-03-13T14:35:10"}
```

---

### `DELETE /api/v1/runs/{run_id}`

Cancel a running or pending pipeline run. Sends SIGTERM to the daemon process.

**Response** `200`:
```json
{
  "run_id": "a3f8c2d1",
  "cancelled": true
}
```

**Errors:** `404` — run not found. `409` — run already in a terminal state.

---

## 4. Webhooks & Triggers

### `POST /api/v1/webhooks/{trigger_id}`

Receive an incoming webhook payload and fire the associated pipeline.

**Flow:**
1. Look up the trigger configuration by `trigger_id`.
2. Verify HMAC-SHA256 signature (if `secret` is configured) via `X-Hub-Signature-256` header.
3. Check per-trigger rate limit (sliding 60-second window).
4. Evaluate trigger `filters` against the payload — skip if no match.
5. Apply `input_map` to transform payload fields into pipeline input variables.
6. Resolve and validate the template.
7. Launch the pipeline.

**Request:** Raw JSON body (the webhook payload from GitHub, Slack, etc.).

**Request headers:**
| Header | Description |
|---|---|
| `X-Hub-Signature-256` | HMAC-SHA256 signature (`sha256=<hex>`) — required when trigger has a `secret` |

**Response** `201`: [RunResponse](#runresponse-schema) (for `async` and `sync` modes).

**Response** `200`: `{"status": "accepted", "run_id": "..."}` (for `fire_and_forget` mode).

**Response** `200`: `{"status": "skipped", "reason": "..."}` (when trigger is disabled or filters don't match).

**Errors:** `403` — invalid signature. `404` — trigger not found. `429` — rate limit exceeded.

---

### `POST /api/v1/triggers`

Create a new webhook trigger.

**Request body:**
```json
{
  "id": "my-trigger",
  "template_id": "coding-pipeline-v1",
  "mode": "async",
  "secret": "my-shared-secret",
  "rate_limit": 10,
  "input_map": {
    "repo": "$.repository.full_name",
    "branch": "$.ref",
    "env": "production"
  },
  "filters": [
    { "field": "$.ref", "operator": "eq", "value": "refs/heads/main" }
  ],
  "enabled": true
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | string | auto-generated | Trigger identifier |
| `template_id` | string | required | Pipeline template to run |
| `mode` | string | `"async"` | `"sync"`, `"async"`, or `"fire_and_forget"` |
| `secret` | string | null | HMAC-SHA256 shared secret (write-only — never returned) |
| `rate_limit` | int | `0` | Max requests/minute (0 = unlimited) |
| `input_map` | dict | `{}` | Maps payload fields to pipeline input vars. `$.path` = dot-path, `{{payload.path}}` = template, others = literal |
| `filters` | list | `[]` | Conditions evaluated against the payload |
| `enabled` | bool | `true` | Whether this trigger is active |

**Response** `201`: [TriggerResponse](#triggerresponse-schema)

**Errors:** `400` — validation failed. `409` — trigger ID already exists.

---

### `GET /api/v1/triggers`

List webhook triggers with optional filtering.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `template_id` | string | null | Filter by template ID |
| `mode` | string | null | Filter by execution mode |
| `enabled` | string | null | `"true"` or `"false"` |
| `limit` | int | `100` | Page size |
| `offset` | int | `0` | Pagination offset |

**Response** `200`:
```json
{
  "items": [ /* TriggerResponse objects */ ]
}
```

---

### `GET /api/v1/triggers/{trigger_id}`

Get a single trigger by ID.

**Response** `200`: [TriggerResponse](#triggerresponse-schema)

**Errors:** `404` — not found.

---

### `PUT /api/v1/triggers/{trigger_id}`

Update an existing trigger. Only provided fields are updated; omitted fields retain current values.

**Request body:**
```json
{
  "rate_limit": 20,
  "enabled": false
}
```

All fields optional: `mode`, `secret`, `rate_limit`, `input_map`, `filters`, `enabled`.

**Response** `200`: [TriggerResponse](#triggerresponse-schema)

**Errors:** `400` — validation failed. `404` — not found.

---

### `DELETE /api/v1/triggers/{trigger_id}`

Delete a trigger.

**Response** `204`: No content.

**Errors:** `404` — not found.

---

## 5. Human Reviews

Pipeline runs that finish with a confidence score in the human-review tier enter `pending_review` status. These endpoints manage that review queue.

### `GET /api/v1/reviews`

List pipeline runs awaiting human review. Returns enriched records with confidence score, routing tier, and justification.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `20` | Page size (max 100) |
| `offset` | int | `0` | Pagination offset |

**Response** `200`:
```json
{
  "items": [
    {
      "run_id": "a3f8c2d1",
      "template_id": "coding-pipeline-v1",
      "status": "pending_review",
      "created_at": "2026-03-13T14:30:22",
      "completed_at": "2026-03-13T14:35:10",
      "review_reason": "Score 0.82 below auto-merge threshold",
      "reviewed_at": null,
      "reviewed_by": null,
      "confidence_score": 0.82,
      "tier_name": "human_review",
      "action": "human_review",
      "justification": "Score in human review range [0.70, 0.95)"
    }
  ],
  "total": 3,
  "limit": 20,
  "offset": 0
}
```

---

### `POST /api/v1/reviews/{run_id}/approve`

Approve a pending-review run. Transitions status from `pending_review` → `success`.

**Request body:**
```json
{
  "reviewed_by": "conny",
  "note": "Looks good, minor formatting only"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `reviewed_by` | string | no | Operator identifier (for audit trail) |
| `note` | string | no | Approval note |

**Response** `200`: [RunResponse](#runresponse-schema) (updated).

**Errors:** `404` — run not found. `409` — run is not in `pending_review` status.

---

### `POST /api/v1/reviews/{run_id}/reject`

Reject a pending-review run. Transitions status from `pending_review` → `rejected`.

**Request body:**
```json
{
  "reason": "Quality too low for publication",
  "reviewed_by": "conny"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `reason` | string | **yes** | Rejection reason |
| `reviewed_by` | string | no | Operator identifier |

**Response** `200`: [RunResponse](#runresponse-schema) (updated).

**Errors:** `404` — run not found. `409` — run is not in `pending_review` status.

---

## 6. Cost Tracking

### `GET /api/v1/costs/summary`

Aggregated cost data grouped by day, template, or model.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `start_date` | string | null | ISO date `YYYY-MM-DD` (inclusive lower bound) |
| `end_date` | string | null | ISO date `YYYY-MM-DD` (inclusive upper bound) |
| `group_by` | string | `"day"` | Grouping dimension: `"day"`, `"template"`, or `"model"` |
| `limit` | int | `20` | Page size (max 100) |
| `offset` | int | `0` | Pagination offset |

**Response** `200`:
```json
{
  "items": [
    { "date": "2026-03-13", "total_cost": 12.45, "total_runs": 8, "total_tokens": 125000 }
  ],
  "total": 30,
  "limit": 20,
  "offset": 0
}
```

**Errors:** `400` — invalid `group_by` or malformed date.

---

### `GET /api/v1/costs/run/{run_id}`

Per-phase cost breakdown for a specific pipeline run.

**Response** `200`:
```json
{
  "run_id": "a3f8c2d1",
  "items": [
    {
      "phase_id": "research",
      "model": "claude-sonnet-4-6",
      "input_tokens": 1200,
      "output_tokens": 800,
      "cost_usd": 0.0032
    }
  ],
  "total_cost": 0.0128,
  "total_input_tokens": 5400,
  "total_output_tokens": 3200
}
```

**Errors:** `404` — no cost records for the run.

---

## 7. Trust Profiles

Trust profiles track per-(repo, template, task_type) trust scores that calibrate auto-merge thresholds.

### `GET /api/v1/trust/profiles`

List all trust profiles.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `100` | Page size (max 500) |
| `offset` | int | `0` | Pagination offset |

**Response** `200`:
```json
{
  "items": [
    {
      "id": 1,
      "repo": "ToscanAI/orchestration-engine",
      "template_id": "coding-pipeline-v1",
      "task_type": "code",
      "trust_score": 0.85,
      "auto_merge_threshold": 0.90,
      "human_review_threshold": 0.70,
      "total_runs": 42,
      "successful_merges": 38,
      "regressions": 1,
      "reverted_prs": 0,
      "last_run_at": "2026-03-13T14:30:22",
      "created_at": "2026-02-20T10:00:00",
      "updated_at": "2026-03-13T14:30:22"
    }
  ],
  "total": 5,
  "limit": 100,
  "offset": 0
}
```

---

### `GET /api/v1/trust/profiles/{profile_id}`

Get a single trust profile by integer primary key.

**Response** `200`: Trust profile object (same shape as list items).

**Errors:** `404` — profile not found.

---

### `PUT /api/v1/trust/profiles/{profile_id}`

Manually override a trust score. Re-derives the `auto_merge_threshold` and logs a `trust_adjustments` audit entry.

**Request body:**
```json
{
  "trust_score": 0.75,
  "reason": "Lowered after regression incident",
  "reviewed_by": "conny"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `trust_score` | float | **yes** | New score in `[0.0, 1.0]` |
| `reason` | string | **yes** | Justification for the override |
| `reviewed_by` | string | no | Operator identifier |

**Response** `200`: Updated trust profile object.

**Errors:** `404` — profile not found. `422` — score out of range.

---

### `GET /api/v1/trust/adjustments`

Return the trust adjustment audit log for a profile.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `profile_id` | int | **required** | Trust profile primary key |
| `limit` | int | `100` | Page size (max 500) |
| `offset` | int | `0` | Pagination offset |

**Response** `200`:
```json
{
  "items": [
    {
      "id": 7,
      "profile_id": 1,
      "delta": -0.10,
      "reason": "manual_override:conny",
      "run_id": null,
      "score_before": 0.85,
      "score_after": 0.75,
      "created_at": "2026-03-13T15:00:00"
    }
  ],
  "total": 7,
  "limit": 100,
  "offset": 0
}
```

**Errors:** `404` — profile not found.

---

## 8. Integrations

### `POST /api/v1/telegram/callback`

Handle Telegram Bot API webhook updates for HITL inline keyboard actions (Approve / Reject buttons).

**Security:** Requires `NOTIFY_TELEGRAM_WEBHOOK_SECRET` environment variable. The `X-Telegram-Bot-Api-Secret-Token` header must match.

**Request:** Raw Telegram `callback_query` update JSON.

**Response** `200`:
```json
{
  "ok": true,
  "action": "approve",
  "run_id": "a3f8c2d1"
}
```

**Errors:** `403` — invalid secret token. `400` — invalid JSON or unparseable callback data.

---

### `POST /api/v1/github/issues`

Receive GitHub `issues` webhook events and launch pipelines automatically via IssueAutomation.

**Trigger conditions:**
- `X-GitHub-Event: issues`
- `action == "opened"` with the `orchemist` label already present, OR
- `action == "labeled"` with the `orchemist` label being applied

The trigger label is configurable via the `ISSUE_TRIGGER_LABEL` environment variable (default `"orchemist"`).

**Flow:**
1. Validate event type and action.
2. Check for the trigger label.
3. Deduplicate — skip if an active pipeline run already exists for this issue.
4. Classify the issue, select a template, extract inputs.
5. Launch pipeline via daemon infrastructure.
6. Post a GitHub comment with the run ID (best-effort).

**Security:** Optionally verifies `X-Hub-Signature-256` when the GitHub App `webhook_secret` is configured.

**Response** `202`:
```json
{
  "status": "accepted",
  "run_id": "a3f8c2d1",
  "classification": "bug",
  "template_id": "coding-pipeline-v1",
  "comment_url": "https://github.com/org/repo/issues/42#issuecomment-123"
}
```

**Response** `200`: `{"status": "ignored", "reason": "..."}` or `{"status": "skipped", "reason": "active_run_exists", "run_id": "..."}`.

**Errors:** `400` — invalid JSON or missing payload fields. `403` — invalid signature.

---

### `POST /api/v1/github/issues/pipeline-ready`

Receive GitHub `issues` webhook events for the `pipeline-ready` label specifically. Launches `coding-pipeline-v1` immediately.

**Trigger conditions:**
- `X-GitHub-Event: issues`
- `action == "labeled"` with `label.name == "pipeline-ready"`

**Flow:**
1. Validate event and label.
2. Deduplicate.
3. Generate pipeline input from issue data.
4. Launch `coding-pipeline-v1`.
5. Remove the `pipeline-ready` label (best-effort).
6. Post a comment with run ID and branch name.

**Response** `202`:
```json
{
  "status": "accepted",
  "run_id": "a3f8c2d1",
  "branch_name": "feat/coding-pipeline-v1-a3f8c2d1"
}
```

**Response** `200`: Ignored or skipped (same as above).

**Errors:** `400` — invalid payload. `403` — invalid signature.

---

## Response Schemas

### RunResponse Schema

Returned by all run-related endpoints.

```json
{
  "run_id": "a3f8c2d1",
  "template_id": "coding-pipeline-v1",
  "template_path": "/home/user/.orch/templates/coding-pipeline-v1.yaml",
  "mode": "standalone",
  "status": "running",
  "current_phase": "implement",
  "completed_phases": ["spec", "acceptance_test"],
  "pid": 12345,
  "output_dir": "/home/user/output/coding-pipeline-v1-20260313-143022-a3f8c2d1",
  "error_message": null,
  "gateway_url": null,
  "skip_scoring": false,
  "scoring_status": null,
  "scoring_score": null,
  "started_at": "2026-03-13T14:30:22",
  "completed_at": null,
  "created_at": "2026-03-13T14:30:20",
  "parent_run_id": null,
  "chain_depth": 0,
  "review_reason": null,
  "reviewed_at": null,
  "reviewed_by": null
}
```

| Field | Type | Description |
|---|---|---|
| `run_id` | string | 8-character UUID prefix |
| `template_id` | string | Template `id` field |
| `template_path` | string | Absolute path to the template YAML |
| `mode` | string | Execution mode: `standalone`, `openclaw`, `dry-run` |
| `status` | string | `pending`, `running`, `success`, `failed`, `cancelled`, `crashed`, `scoring_failed`, `pending_review`, `rejected` |
| `current_phase` | string \| null | Currently executing phase ID |
| `completed_phases` | list[string] | Phase IDs that have finished |
| `pid` | int \| null | Daemon process ID |
| `output_dir` | string | Directory containing phase outputs |
| `error_message` | string \| null | Error details (on failure) |
| `gateway_url` | string \| null | OpenClaw gateway URL used |
| `skip_scoring` | bool | Whether scoring was skipped |
| `scoring_status` | string \| null | `"passed"`, `"failed"`, `"error"`, or null |
| `scoring_score` | float \| null | Composite score (0.0–1.0) |
| `started_at` | string \| null | ISO timestamp |
| `completed_at` | string \| null | ISO timestamp |
| `created_at` | string \| null | ISO timestamp |
| `parent_run_id` | string \| null | Parent run (for chained pipelines) |
| `chain_depth` | int | Chaining depth (0 = root) |
| `review_reason` | string \| null | Why the run entered review |
| `reviewed_at` | string \| null | When the review was completed |
| `reviewed_by` | string \| null | Who reviewed the run |

### TriggerResponse Schema

Returned by all trigger-related endpoints.

```json
{
  "id": "my-trigger",
  "template_id": "coding-pipeline-v1",
  "mode": "async",
  "secret": "***",
  "rate_limit": 10,
  "input_map": { "repo": "$.repository.full_name" },
  "filters": [],
  "enabled": true,
  "created_at": "2026-03-13T14:30:22"
}
```

| Field | Type | Description |
|---|---|---|
| `id` | string | Trigger identifier |
| `template_id` | string | Pipeline template to run |
| `mode` | string | `"sync"`, `"async"`, or `"fire_and_forget"` |
| `secret` | string \| null | Always `"***"` when set (write-only) |
| `rate_limit` | int | Max requests/minute (0 = unlimited) |
| `input_map` | dict | Payload-to-input variable mapping |
| `filters` | list | Payload filter conditions |
| `enabled` | bool | Whether the trigger is active |
| `created_at` | string \| null | ISO timestamp |

---

## Environment Variables

| Variable | Used by | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Pipeline execution | API key for standalone mode |
| `OPENCLAW_GATEWAY_URL` | Pipeline execution, webhooks | Gateway URL for openclaw mode |
| `OPENCLAW_GATEWAY_TOKEN` | Pipeline execution | Gateway auth token |
| `REGRESSION_TRIGGER_ID` | `GET /health/webhook` | Trigger ID to check (default `"regression-ci-trigger"`) |
| `ISSUE_TRIGGER_LABEL` | `POST /github/issues` | Label name that triggers automation (default `"orchemist"`) |
| `ISSUE_CLASSIFY_CONFIDENCE_THRESHOLD` | `POST /github/issues` | Min classification confidence (default `0.70`) |
| `NOTIFY_TELEGRAM_BOT_TOKEN` | `POST /telegram/callback` | Telegram bot token |
| `NOTIFY_TELEGRAM_CHAT_ID` | `POST /telegram/callback` | Telegram chat ID |
| `NOTIFY_TELEGRAM_WEBHOOK_SECRET` | `POST /telegram/callback` | Shared secret for Telegram webhook |

---

## HTTP Status Codes

| Code | Meaning |
|---|---|
| `200` | Success (or ignored/skipped for webhooks) |
| `201` | Created (new run, template, or trigger) |
| `202` | Accepted (async processing started) |
| `204` | No content (successful delete) |
| `400` | Bad request (invalid input, template error) |
| `403` | Forbidden (invalid signature, read-only template) |
| `404` | Not found |
| `409` | Conflict (duplicate ID, wrong state for operation) |
| `422` | Validation error (YAML parse, schema, out-of-range) |
| `429` | Rate limit exceeded |
