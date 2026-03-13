# Forensic Finding 01 — Documentation Drift

> **Severity:** 🔴 Critical  
> **Impact:** Contributors and users reading docs will form incorrect assumptions about the system  
> **Generated:** 2026-03-13

---

## Summary

Multiple documentation files contain claims that directly contradict the current implementation. These are not "future items" or "aspirational" — they are factually wrong statements about how the system works today.

---

## Finding 1.1: `tech-stack.md` Claims "No Framework Dependencies"

**Document:** `docs/tech-stack.md`  
**Claim:**

> "The engine has **no FastAPI, Django, Flask, Celery, or similar framework dependencies**. This is deliberate."

The document includes a rejection table:

| Framework | Why rejected |
|---|---|
| FastAPI | "Adds async runtime, HTTP server, and 10+ transitive deps; unnecessary for a local CLI tool" |

**Reality:**

- `pyproject.toml` defines a `[web]` optional dependency group: `fastapi>=0.100.0`, `uvicorn[standard]>=0.20.0`, `sse-starlette>=1.0.0`
- `src/orchestration_engine/web/api.py` is a full FastAPI application with **33 endpoints** across health, templates, runs, webhooks, triggers, reviews, costs, trust, telegram, and GitHub integrations
- `src/orchestration_engine/web/app.py` serves the web UI via FastAPI
- `orch serve` and `orch api-server` are first-class CLI commands that launch FastAPI
- The web UI is prominently featured in the README: "✅ Local web UI — browse templates, start runs, and watch live progress"

**Verdict:** The claim is technically defensible (FastAPI is optional) but **factually misleading**. FastAPI is not a rejected technology — it is a core feature of the product. The doc reads as though the decision was made and the feature doesn't exist, when in fact the opposite is true.

**Recommendation:** Rewrite the "No Framework Dependencies" section to explain the optional-dependency architecture. Acknowledge that FastAPI powers the web UI and REST API as an opt-in layer, while the core CLI + library remains framework-free.

---

## Finding 1.2: `ARCHITECTURE.md` Claims Serial Execution Only

**Document:** `docs/ARCHITECTURE.md`  
**Claim:**

> "If you have two phases that don't depend on each other, they're identified as independent — **though the current version runs them one at a time.**"

**Reality:**

- `sequencer.py` implements parallel wave execution via `ThreadPoolExecutor`
- `PipelineTemplate` has a `parallel` field (default `True`) and `max_parallel` field
- `_execute_wave_parallel()` method runs independent phases concurrently with thread-safe `_phase_outputs_lock` (RLock)
- The `fail_fast` flag enables early abort propagation via `threading.Event`
- The README itself lists: "✅ Phase sequencing with dependency graphs — topological sort, parallel wave execution"

**Verdict:** Parallel execution has been fully implemented since Issue #102. The ARCHITECTURE.md description is stale and contradicts both the code and the README.

**Recommendation:** Update ARCHITECTURE.md to reflect parallel execution. Describe the parallel/serial toggle, `max_parallel`, and `fail_fast` behavior.

---

## Finding 1.3: `ARCHITECTURE.md` Lists Only 4 Executors (5 Exist)

**Document:** `docs/ARCHITECTURE.md`  
**Claim:** Lists 4 executors: `AnthropicExecutor`, `OpenClawExecutor`, `DryRunExecutor`, `FallbackHandler`

**Reality:** There are **5** executors:

| Executor | Documented? |
|----------|:-----------:|
| `AnthropicExecutor` | ✅ |
| `OpenClawExecutor` | ✅ |
| `OpenAICompatibleExecutor` | ❌ Missing |
| `DryRunExecutor` | ✅ |
| `FallbackHandler` | ✅ |

The `OpenAICompatibleExecutor` (in `openai_executor.py`) supports Gemini-via-proxy, Ollama, LM Studio, and any OpenAI Chat Completions-compatible endpoint. It is documented in `api-reference.md` but not in the architecture doc.

**Verdict:** The architecture diagram's executor table is incomplete. The `OpenAICompatibleExecutor` is a production-grade executor file with full error handling.

**Recommendation:** Add `OpenAICompatibleExecutor` to the ARCHITECTURE.md executor table and architecture diagram.

---

## Finding 1.4: `GETTING_STARTED.md` Claims Serial-Only Execution

**Document:** `docs/GETTING_STARTED.md`  
**Claim:**

> "Phases in the same wave are independent and could theoretically run in parallel."
> "Parallel Pipelines: ... The current version runs them serially for simplicity, but the structure is ready for concurrency when you need it."

**Reality:** Same as Finding 1.2 — parallel execution is fully implemented and enabled by default.

**Recommendation:** Remove the "could theoretically" and "serially for simplicity" qualifiers. Describe parallel execution as the default behavior.

---

## Finding 1.5: `structured-schemas.md` Lists 5 TaskTypes (14 Exist)

**Document:** `docs/structured-schemas.md`  
**Contains:** A self-acknowledged "Partially outdated" warning, but the gap is larger than implied.

**Documented TaskType values (5):**
`CONTENT`, `CODE`, `RESEARCH`, `TRANSLATION`, `REVIEW`

**Actual values in `schemas.py` (14):**
`CONTENT`, `CODE`, `RESEARCH`, `TRANSLATION`, `REVIEW`, `TRIAGE`, `ANALYSIS`, `COMPLIANCE`, `FINANCIAL`, `SALES`, `SUPPORT`, `COMMAND`, `ACCEPTANCE_RUN`

**Missing from docs:** 9 task types added for knowledge-work pipelines (Issue #123) and behavioral validation (Sprint 7).

**Recommendation:** Update the TaskType table to list all 14 values with descriptions of when each is used.

---

## Finding 1.6: `web-ui.md` Documents Only `/api/` Endpoints (33 `/api/v1/` Endpoints Exist)

**Document:** `docs/web-ui.md`  
**Documented endpoints (8):** All under `/api/` (no version prefix) — `health`, `templates`, `run`, `run/{id}/status`, `run/{id}/outputs`, `run/{id}/resume`, `run/{id}/edit`

**Actual endpoints in `api.py` (33):** All under `/api/v1/` — covering health, templates (CRUD), runs (CRUD + stream + children + logs), webhooks, triggers (CRUD), reviews (list + approve + reject), costs (summary + per-run), trust profiles (CRUD + adjustments), telegram callback, and GitHub issue automation.

The `/api/` routes (from `app.py`) and `/api/v1/` routes (from `api.py`) are two separate API surfaces. The versioned API is undocumented outside of its code docstrings.

**Verdict:** The web-ui.md only documents the original app.py routes. The entire versioned REST API is invisible to documentation readers.

**Recommendation:** Either expand web-ui.md or create a new `rest-api.md` document covering all `/api/v1/` endpoints. See `docs/forensics/07-api-endpoint-audit.md` for the complete list.

---

## Finding 1.7: `api-reference.md` — Stale CLI Command Inventory

**Document:** `docs/api-reference.md`  
**Documented CLI commands:** `orch submit`, `orch status`, `orch list`, `orch cancel`, `orch retry`, `orch dead-letter`, `orch health`, `orch execute`, `orch watch`, `orch workers`, `orch quickstart`, `orch start`, `orch templates`, `orch run`, `orch validate`, `orch list-phases`

**Undocumented CLI commands that exist:**
| Command | Purpose |
|---------|---------|
| `orch launch` | Background daemon execution |
| `orch wait` | Block until pipeline completes |
| `orch serve` | Web UI server |
| `orch ui` | Static frontend server |
| `orch api-server` | REST API only (no frontend) |
| `orch logs` | Daemon log viewer with `--follow` |
| `orch gate` | Merge gate management (list/approve/reject/info) |
| `orch new` | Template scaffolding wizard |
| `orch import` | Markdown → YAML plugin conversion |
| `orch chain` | Chain monitoring |
| `orch children` | List child pipeline runs |
| `orch rubric` | Skill → rubric YAML conversion |
| `orch reviews` | Human review queue management |
| `orch resume` | Declared stub (v2 feature) |
| `orch scenario` | Scenario runner |

**Verdict:** 15 implemented CLI commands are missing from the API reference. The documented set represents roughly 50% of actual commands.

**Recommendation:** Add all missing commands to api-reference.md. Group by category (pipeline execution, template management, review/gate, monitoring, utilities).

---

## Finding 1.8: `task-queue.md` Documents 4 DB Tables (22+ Exist)

**Document:** `docs/task-queue.md`  
**Documented tables (4):** `tasks`, `task_runs`, `orchestras`, `dead_letter_queue`

**Actual tables in `db.py` (22+):** See `docs/forensics/06-database-schema-drift.md` for detailed coverage.

**Recommendation:** Update task-queue.md or create a dedicated `database-schema.md` document.

---

## Finding 1.9: README YAML Example Uses Non-Standard Syntax

**Document:** `README.md`  
**Claim:** The "What Is It?" quickstart YAML uses `{{brief}}` and `{{research.output}}` syntax:

```yaml
phases:
  research:
    prompt: "Research the topic: {{brief}}"
    model_tier: haiku
  draft:
    prompt: "Write a 500-word article based on: {{research.output}}"
```

**Reality:** Actual template YAML uses a different format:
- Phase definitions are a list of objects with `id`, `prompt_template`, `model_tier`, etc.
- Variable interpolation uses Python `str.format()` syntax: `{input[topic]}`, `{previous_output[research]}`
- The `{{}}` syntax shown is not how templates actually work

**Verdict:** The README quickstart YAML is illustrative shorthand that does not match the actual template format. A user copying this YAML verbatim will get validation errors.

**Recommendation:** Either add a note ("simplified for illustration — see template-authoring.md for actual format") or replace with a valid minimal template that works verbatim.

---

## Finding 1.10: `error-recovery.md` References RecoveryManager API That Differs From Implementation

**Document:** `docs/error-recovery.md`  
**Claim:** Documents `RecoveryManager` with methods: `handle_task_failure()`, `handle_task_success()`, `get_retry_queue()`, `mark_retry_executed()`, `get_error_statistics()`

**Reality:** The `recovery.py` module implements `RecoveryManager` with the documented methods, BUT the error recovery landscape has expanded significantly beyond what this doc describes:

- `diagnosis.py` — LLM-powered failure classification (8 failure classes) supersedes the keyword-pattern classifier described in the doc
- `adaptive_retry.py` — Strategy-based retry (6 strategies) superseding the simple model-escalation-only approach in the doc
- The doc describes `ErrorType` and `ErrorSeverity` accurately but makes no mention of `DiagnosisEngine` or `AdaptiveRetryEngine` which are now the primary recovery path

**Verdict:** The doc describes the Level 3 recovery system accurately. The Level 4 recovery system (diagnosis → adaptive retry) is layered on top and completely undocumented.

**Recommendation:** Add a "Level 4 Recovery" section covering `DiagnosisEngine`, `AdaptiveRetryEngine`, and how they interact with the base `RecoveryManager`.
