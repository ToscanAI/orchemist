# Orchemist Configuration Surface

Every file under `~/.orchestration-engine/` (or wherever the engine's
default project directory resolves), what it controls, who writes it,
and how to audit changes. Closes #838 — addresses risk #2 from the
[2026-05-25 strategic audit](./strategic-audit-2026-05-25/RISK_ASSESSMENT.md)
(operator-edited admin surface).

---

## TL;DR

| File / dir | Purpose | Who writes | Audit trail | Sensitivity |
|---|---|---|---|---|
| `engine.db` | Primary SQLite (WAL) — all runs, gates, audit log | Engine daemons + admin API | Internal table-level | **HIGH** — loses all run history if deleted |
| `engine.db-wal` / `engine.db-shm` | WAL & shared-memory companions to `engine.db` | SQLite | n/a | Tied to `engine.db` |
| `admin.json` | Admin state: autonomy level, feature flags, modes | Admin API (`PUT /api/v1/admin/feature-flags`) + manual edit | **admin_audit_log table (#838)** | **HIGH** — controls runtime feature gates (#840) |
| `tasks/` | Task-queue working directory | `runner.py` | Per-task JSON manifest | LOW — recoverable |
| `webhook-secret` | HMAC secret for `/api/v1/webhooks/*` signature verification | Manual (one-time setup) | None | **CRITICAL** — leaks let any attacker spoof webhooks |
| `pid` / `*.pid` | Lockfiles for the engine server + daemons | Engine processes | n/a | Stale on crash; safe to remove if no live process |
| `db_backups/` (optional) | Operator-created `engine.db` backups | Manual | n/a | Treat as sensitive (full history) |

If the operator overrides the engine's project directory (via
`--db-path`, the `ORCH_DB_PATH` env var, or by symlinking
`~/.orchestration-engine/`), the same files appear at the override
location with identical sensitivity.

---

## Default base path

`~/.orchestration-engine/` (resolved per-process from `Path.home()`).
Honoured by:

- `db.py:52` (`engine.db` default)
- `web/api.py:33`, `cli.py:1773`, `mcp/tools.py:31` (project-dir
  default for tasks, secrets, etc.)
- `daemon.py:1889` (daemon DB path)
- `feature_flags.py:79` (`admin.json` default, overridable via
  `ORCH_ADMIN_PATH`)

If you need to relocate the surface (e.g. mount on a separate
filesystem for `engine.db`), set both `ORCH_DB_PATH` and
`ORCH_ADMIN_PATH` — together they cover the two persistent surfaces
(`engine.db` and `admin.json`).

---

## File-by-file detail

### `engine.db` (SQLite, WAL mode)

The single source of truth for everything the engine remembers across
restarts: pipeline runs, tasks, dead-letter queue, regressions, trust
profiles, review outcomes, sprint chain state, and (post-#838) the
admin audit log.

- **Writer:** every daemon + the admin API
- **Reader:** all CLI commands, web API, MCP tools
- **Permissions:** owned by the operator UID; default `0644`. Anyone with
  read access to `~/.orchestration-engine/` can read every run's
  prompts, outputs, and cost data. Do **not** publish this file.
- **Backup:** there is **no built-in backup mechanism today** (see
  forensics_2026-03-13/09-post-remediation-advisory.md §4). The operator
  is responsible for periodic `cp engine.db db_backups/engine.YYYY-MM-DD.db`
  while WAL is checkpointed (`PRAGMA wal_checkpoint(TRUNCATE);`).
- **Concurrent writers:** WAL handles multiple readers + one writer
  natively. Multi-writer concurrency (multiple daemons in parallel) is
  not bounded — see #839 (SQLite WAL concurrent-daemon backpressure).

### `admin.json`

Operator-edited (or admin-API-edited) JSON document with three keys:

```json
{
  "autonomy_level": "4.3",
  "feature_flags": {
    "phase0_hard_gate": false,
    "extend_verdict": true,
    "dialogue_phase": false,
    "cross_repo": false
  },
  "modes": {
    "openrouter": true,
    "standalone": true,
    "openclaw": false,
    "dry_run": true
  }
}
```

- **Writers:** the admin API (`PUT /api/v1/admin/feature-flags`,
  validated by `_coerce_admin_doc()` in `web/api.py`), AND the operator
  by hand. The path is overridable via `ORCH_ADMIN_PATH`.
- **Readers:** the admin API GET endpoint (frontend hydration) AND
  `feature_flags.py` at runtime (#840). The runtime reader caches for
  30 seconds; a hand-edit takes up to 30s to land for in-flight
  pipelines.
- **Validation:** every write goes through `_coerce_admin_doc` which
  re-applies per-field type defaults — but hand-edits bypass this until
  the next admin-API read happens. A malformed hand-edit can leave
  pipelines reading defaults silently.
- **Audit log (#838):** every successful `PUT /api/v1/admin/feature-flags`
  appends a row to the `admin_audit_log` table in `engine.db`
  recording (before, after, source_pid, action verb, timestamp).
  Inspect via `GET /api/v1/admin/audit-log` or:

  ```bash
  sqlite3 ~/.orchestration-engine/engine.db \
    "SELECT created_at, action, target, before_json, after_json, source_pid \
     FROM admin_audit_log ORDER BY id DESC LIMIT 20;"
  ```

- **Hand-edits are NOT audited.** If you edit `admin.json` directly
  with `$EDITOR`, no row is appended. Workaround: prefer the admin UI
  or a `curl PUT` so the audit trail is preserved.
- **Sensitivity:** HIGH — controls trust gates, autonomy ramp, feature
  toggles. Should be `chmod 600` if the host is shared.

### `tasks/` directory

Per-task working files (JSON manifests, intermediate output). Written
by `runner.py:512`. Each task gets its own subdirectory keyed by task
id; auto-cleaned on completion.

- **Writer:** the task runner
- **Reader:** debug only; runtime never re-reads
- **Sensitivity:** LOW — task prompts may contain issue bodies, but the
  same data lives in `engine.db`

### `webhook-secret`

HMAC secret used to verify `X-Hub-Signature-256` headers on
`/api/v1/webhooks/{trigger_id}` requests.

- **Writer:** **operator manually**, one-time at setup. Not managed by
  any engine code today.
- **Reader:** `web/api.py:_verify_github_signature` and equivalents.
- **Permissions:** MUST be `chmod 600`. Leaks let any attacker post
  fake webhook payloads with valid HMAC, triggering arbitrary pipeline
  launches.
- **Rotation:** rewrite the file + restart any daemons; in-flight
  webhooks signed with the old secret will fail signature verification
  (intended).

### `pid` / `*.pid` files

PID lockfiles for the engine server + daemon processes. Auto-created
on launch, removed on clean shutdown. After a crash they linger — safe
to delete if `ps` confirms the PID is gone.

---

## Inspecting the audit log

After any admin-API mutation, query `engine.db`:

```bash
# 20 most recent admin changes
sqlite3 ~/.orchestration-engine/engine.db <<'SQL'
SELECT
  datetime(created_at) AS at,
  action,
  target,
  source_pid,
  before_json,
  after_json
FROM admin_audit_log
ORDER BY id DESC
LIMIT 20;
SQL
```

Or via the REST endpoint:

```bash
curl -s "http://localhost:8000/api/v1/admin/audit-log?limit=20" | jq .
```

Each row records exactly which feature_flag keys changed (the `target`
column is a comma-separated list of changed keys). The `source_pid`
column is the OS pid of the FastAPI worker that served the PUT — not a
user id (the engine has no per-user auth today), but useful for
distinguishing "the admin UI did it" from "a long-running script did it"
when you have multiple workers logged.

---

## What is NOT yet documented / enforced

Tracked but not yet implemented:

- **`chmod 600` not enforced at engine startup.** A future audit
  command should warn (or refuse to launch) when `admin.json` or
  `webhook-secret` is world-readable.
- **Hand-edits to `admin.json` skip the audit log.** A future
  filesystem watcher could append a `direct_edit` audit row when the
  mtime changes without a matching API call.
- **`engine.db` backup is operator-managed.** A `db_backups/` rotation
  policy or built-in `orch backup` command is desirable.
- **No `~/.orchestration-engine/` directory validation on startup.**
  Bogus files in the directory (rogue subdirs, large logs) accumulate
  silently.

---

*Last reviewed: 2026-05-25 (closes #838).*
