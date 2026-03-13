# Forensic Finding 07 — REST API Endpoint Audit

> **Severity:** 🟡 High  
> **Impact:** 33 production REST API endpoints have no formal documentation  
> **Generated:** 2026-03-13

---

## Summary

The engine exposes **two independent API surfaces**:

1. **`web/app.py`** — The web UI API at `/api/` (documented in `web-ui.md`)
2. **`web/api.py`** — The versioned REST API at `/api/v1/` (undocumented)

`docs/web-ui.md` covers only the first surface (8 endpoints). The second surface (33 endpoints) has zero formal documentation — its behavior is only discoverable by reading the FastAPI source code or the auto-generated OpenAPI schema at `/api/v1/docs`.

---

## Documented API Surface (`web/app.py` — `/api/`)

| Method | Path | Documented In |
|--------|------|:-------------:|
| `GET` | `/api/health` | `web-ui.md` ✅ |
| `GET` | `/api/templates` | `web-ui.md` ✅ |
| `GET` | `/api/templates/{name}` | `web-ui.md` ✅ |
| `POST` | `/api/run` | `web-ui.md` ✅ |
| `GET` | `/api/run/{run_id}/status` | `web-ui.md` ✅ |
| `GET` | `/api/run/{run_id}/outputs` | `web-ui.md` ✅ |
| `POST` | `/api/run/{run_id}/resume` | `web-ui.md` ✅ |
| `POST` | `/api/run/{run_id}/edit` | `web-ui.md` ✅ |

---

## Undocumented API Surface (`web/api.py` — `/api/v1/`)

### Health
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/health` | Health check |
| `GET` | `/api/v1/health/webhook` | Webhook system health |

### Templates (CRUD)
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/templates` | List all templates |
| `GET` | `/api/v1/templates/{name}` | Get template detail |
| `POST` | `/api/v1/templates` | Create new template |
| `POST` | `/api/v1/templates/validate` | Validate template YAML |
| `PUT` | `/api/v1/templates/{name}` | Update existing template |
| `DELETE` | `/api/v1/templates/{name}` | Delete template |

### Pipeline Runs
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/runs` | Launch a pipeline run |
| `GET` | `/api/v1/runs` | List all runs |
| `GET` | `/api/v1/runs/{run_id}` | Get run status |
| `GET` | `/api/v1/runs/{run_id}/children` | List child pipeline runs |
| `GET` | `/api/v1/runs/{run_id}/logs` | Get run logs |
| `GET` | `/api/v1/runs/{run_id}/stream` | SSE live progress stream |
| `DELETE` | `/api/v1/runs/{run_id}` | Cancel/delete a run |

### Webhooks & Triggers
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/webhooks/{trigger_id}` | Receive webhook payload |
| `POST` | `/api/v1/triggers` | Create trigger config |
| `GET` | `/api/v1/triggers` | List all triggers |
| `GET` | `/api/v1/triggers/{trigger_id}` | Get trigger detail |
| `PUT` | `/api/v1/triggers/{trigger_id}` | Update trigger |
| `DELETE` | `/api/v1/triggers/{trigger_id}` | Delete trigger |

### Human Reviews
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/reviews` | List pending reviews |
| `POST` | `/api/v1/reviews/{run_id}/approve` | Approve a review |
| `POST` | `/api/v1/reviews/{run_id}/reject` | Reject a review |

### Cost Tracking
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/costs/summary` | Cost summary (daily, per-template) |
| `GET` | `/api/v1/costs/run/{run_id}` | Per-run cost breakdown |

### Trust Calibration
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/trust/profiles` | List trust profiles |
| `GET` | `/api/v1/trust/profiles/{profile_id}` | Get trust profile detail |
| `PUT` | `/api/v1/trust/profiles/{profile_id}` | Update trust profile |
| `GET` | `/api/v1/trust/adjustments` | List trust adjustments |

### Integrations
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/telegram/callback` | Telegram HITL callback |
| `POST` | `/api/v1/github/issues` | GitHub issue webhook handler |
| `POST` | `/api/v1/github/issues/pipeline-ready` | Pipeline-ready label handler |

---

## API Surface Comparison

| Category | `/api/` (Documented) | `/api/v1/` (Undocumented) |
|----------|:---:|:---:|
| Health | 1 | 2 |
| Templates | 2 (read-only) | 6 (full CRUD) |
| Runs | 4 | 6 |
| Webhooks/Triggers | 0 | 6 |
| Reviews | 0 | 3 |
| Costs | 0 | 2 |
| Trust | 0 | 4 |
| Integrations | 0 | 3 |
| **Total** | **8** | **33** |

---

## Overlap & Confusion Risk

Both API surfaces serve some of the same resources (templates, runs, health) but at different paths and with different response formats:

| Resource | `/api/` path | `/api/v1/` path |
|----------|-------------|----------------|
| Health | `/api/health` | `/api/v1/health` |
| List templates | `/api/templates` | `/api/v1/templates` |
| Template detail | `/api/templates/{name}` | `/api/v1/templates/{name}` |
| Start run | `POST /api/run` | `POST /api/v1/runs` |
| Run status | `/api/run/{id}/status` (SSE) | `/api/v1/runs/{id}` (JSON) |

A developer consuming the API must know:
- `/api/` is the web UI backend (designed for the Next.js frontend)
- `/api/v1/` is the programmatic REST API (designed for CLI and integrations)

This distinction is not documented anywhere.

---

## Recommendation

1. **Create `docs/rest-api-v1.md`** — Full reference for all 33 `/api/v1/` endpoints with request/response schemas, status codes, and curl examples
2. **Update `docs/web-ui.md`** — Add a note explaining the two API surfaces and when to use which
3. **Add OpenAPI link** — Note that `orch api-server` or `orch serve` exposes Swagger docs at `/api/v1/docs` (FastAPI auto-generates this)
4. **Consider API surface reduction** — Evaluate whether the `/api/` surface (app.py) should be deprecated in favor of `/api/v1/` (api.py) to avoid duplication
