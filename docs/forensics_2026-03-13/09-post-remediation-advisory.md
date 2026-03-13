# Post-Remediation Advisory

> **Date**: 2026-03-13
> **Audience**: Non-technical operator / project owner
> **Context**: This advisory was produced after a full forensic audit and 4-tier documentation remediation of the Orchemist codebase. It captures operational risks, maintenance items, and recommended actions based on deep analysis of every module.

---

## Before First Run

### 1. Budget Controls Are Not Optional

Every pipeline run, every issue classification, every diagnosis, and every retry calls Claude — and each call costs money. A single sprint chain of 12 issues could easily cost **$15–50+** depending on model tiers and retry escalation.

**Action**: Set `max_cost_per_run` and `daily_budget_cap_usd` in every template's `budget:` block and in every sprint queue config. Without them, there is *no spending limit*.

```yaml
# In your pipeline template:
budget:
  max_cost_per_run: 2.00      # USD — abort if exceeded
  max_cost_per_day: 20.00     # USD — reject new launches
  warn_at_percentage: 80.0    # warn at 80% of per-run cap
```

```yaml
# In your sprint_queue.yaml:
daily_budget_cap_usd: 10.00
```

### 2. GitHub CLI Must Be Authenticated

Issue comments, PRs, label management, and webhook registration all shell out to `gh api`. If `gh auth login` hasn't been completed on the machine running the engine, **half the automation silently does nothing** — no errors, just missing comments and unlabeled issues.

**Action**: Run `gh auth login` on the server before deploying. Verify with `gh auth status`.

### 3. The Web API Has No Authentication

The REST API ships with `allow_origins=["*"]` (wide-open CORS) and no API key or bearer token on any endpoint. Anyone who discovers your URL can launch pipelines, create triggers, and consume your API budget.

The only protection is optional HMAC signature verification on GitHub webhook endpoints (opt-in via `webhook_secret` config).

**Action**: If you expose the API publicly, place a reverse proxy (nginx, Caddy, or a cloud load balancer) in front of it with authentication. Never expose the raw FastAPI port to the internet.

---

## Operational Awareness

### 4. SQLite Is a Single-Server Database

Everything — pipeline runs, diagnoses, cost tracking, issue mappings, chain state, webhook triggers, trust profiles — lives in one file: `~/.orchestration-engine/engine.db`.

**Implications**:
- **No automated backup exists.** A disk failure loses all run history, cost records, and chain state.
- **It cannot span multiple servers.** Horizontal scaling would require migrating to PostgreSQL or similar.
- **WAL mode is enabled** (good for concurrent reads), but heavy parallel writes from many daemons can still contend on the single file.

**Action**: Set up a daily backup of `~/.orchestration-engine/engine.db` (a simple cron job copying the file is sufficient for SQLite in WAL mode). Test restoring from a backup at least once.

### 5. Daemons Are Fire-and-Forget Processes

When you launch a pipeline, the engine spawns a detached subprocess (`start_new_session=True`). If the server reboots, loses power, or the process is killed mid-run, those daemons die silently. The DB rows remain `status='running'` indefinitely — there is no automatic cleanup.

**Action**: Periodically check for orphaned runs:
```bash
orch list                     # look for stale "running" status
orch chain --active           # check for stuck chain processes
```

Consider adding a monitoring check that alerts when any run has been in `running` state for longer than your expected maximum duration (e.g. 2 hours).

### 6. The Retry System Can Compound Costs

When a pipeline fails, the daemon automatically:
1. Runs a Haiku diagnosis call (~$0.05)
2. Plans a retry with potential model escalation
3. Spawns up to **3 retry runs** (default cap)

A retry escalated to Opus costs ~$0.50/run. A single bad issue could trigger:

```
Original run         → fails   → $0.15 (Sonnet)
  + Diagnosis #1     →         → $0.05 (Haiku)
  + Retry #1 (Opus)  → fails   → $0.50
  + Diagnosis #2     →         → $0.05
  + Retry #2 (Opus)  → fails   → $0.50
  + Diagnosis #3     →         → $0.05
  + Retry #3 (Opus)  → fails   → $0.50
  = Total                        $1.80 for one issue
```

Multiply by 12 issues in a sprint chain = **$21.60** worst case. The budget controls from point #1 are what prevent this.

---

## Sprint Chain Risks

### 7. The Sprint Chain Runs Unsupervised

Once the first issue merges successfully, the chain automatically:
1. Labels the next issue `pipeline-ready`
2. That triggers a new pipeline launch
3. That pipeline runs, scores, routes, and auto-merges
4. That merge triggers the next issue in the queue
5. Repeat up to `max_chain_depth: 12`

This is **12 full pipeline cycles** — each with classification, execution, scoring, routing, and potentially retries — running without you looking.

**Guard rails** (your safety net):

| Guard | What It Does | Config |
|-------|-------------|--------|
| **Score threshold** | Stops chain if pipeline output quality drops below threshold | `score_threshold: 0.75` in sprint_queue.yaml |
| **Daily budget cap** | Stops chain if daily spending exceeds limit | `daily_budget_cap_usd: 10.00` in sprint_queue.yaml |
| **Human pause** | Stops chain when a `status='paused'` row exists in DB | Manual DB insert or external tool |
| **Chain depth** | Hard stop after N hops | `max_chain_depth: 12` in template, hard cap `20` |
| **Per-run budget** | Aborts individual run if cost exceeds limit | `max_cost_per_run` in template budget block |

**Action**: Before starting your first sprint chain, verify that:
- You know how to check active chains: `orch chain --active`
- You know how to monitor a specific chain: `orch chain <run_id>`
- You understand how to invoke the human-pause mechanism
- Your budget limits are set and tested

---

## Maintenance Items

### 8. Model Version Strings Are Hardcoded

The model escalation ladder and diagnosis model tier are hardcoded in Python source files:

| Location | Hardcoded Values |
|----------|-----------------|
| `adaptive_retry.py` — `MODEL_ESCALATION_LADDER` | `claude-haiku-4-5-20241022`, `claude-sonnet-4-6`, `claude-opus-4-6` |
| `adaptive_retry.py` — `MODEL_COST_HEURISTIC` | Cost estimates: $0.05, $0.15, $0.50 |
| `diagnosis.py` — `DEFAULT_MODEL_TIER` | `"haiku"` |

When Anthropic retires, renames, or re-prices models, these strings need updating. This requires editing Python files — a code change.

**Action**: Flag this for your developer when Anthropic announces model changes. The cost heuristics may also drift from actual pricing over time.

### 9. Templates Are Your Highest-Leverage Configuration

The YAML templates define *what* your pipelines actually do — the phase prompts, scoring criteria, chaining rules, tool access, and routing config. They are the single most impactful thing you can edit without touching Python code.

Key template documentation:
- [Template Authoring Guide](../template-authoring.md) — syntax reference for all fields
- [Pipeline Chaining](../pipeline-chaining.md) — `on_complete:` block configuration
- [Confidence Scoring](../confidence-scoring.md) — how scoring and routing work

Templates **will** need tuning as you learn what works. Start with `dry-run` mode to test changes before committing real API spend.

### 10. Webhook URLs Must Be Publicly Reachable

GitHub needs to POST events to your server. This requires either:
- A cloud VM with a public IP and a domain/SSL certificate
- A tunnel service (ngrok, Cloudflare Tunnel, Tailscale Funnel)

The `scripts/register_webhook.py` script handles the GitHub-side setup (creating the webhook + HMAC secret), but networking and DNS are infrastructure concerns outside the engine's scope.

**Action**: Decide your hosting strategy before configuring GitHub webhooks. The webhook secret file is stored at `~/.orchestration-engine/webhook-secret` with restricted permissions (owner-only read/write).

---

## Monitoring Cheatsheet

| Command | What It Shows |
|---------|--------------|
| `orch list` | All pipeline runs with status, template, score |
| `orch status <run_id>` | Detailed status of a specific run |
| `orch chain --active` | All currently running chains |
| `orch chain <run_id>` | Full chain tree for any run |
| `orch children <run_id>` | Direct child runs of a parent |
| `orch logs <run_id>` | Daemon log file for a run |
| `orch score <template>` | Run scoring against a template |

---

## Priority Action Items

| Priority | Action | Risk Mitigated |
|----------|--------|---------------|
| **1** | Set `budget:` limits in every template | Runaway spending |
| **2** | Set up daily DB backups | Data loss |
| **3** | Put an auth proxy in front of the API | Unauthorized pipeline launches |
| **4** | Learn `orch list`, `orch status`, `orch chain --active` | Blind operation |
| **5** | Test the human-pause mechanism before running a sprint | No emergency brake when needed |
| **6** | Run `gh auth login` on the server | Silent GitHub integration failures |
| **7** | Decide hosting/tunneling strategy for webhooks | GitHub can't reach your server |
| **8** | Do a dry-run sprint with `mode: dry-run` first | Validate the full chain before real money flows |

---

## Final Assessment

The system is remarkably well-engineered — the guard rails, non-fatal error handling, depth limits, and budget enforcement show careful design. But it is built to be **autonomous**: it will classify issues, launch pipelines, retry failures, merge PRs, and advance sprint queues while you sleep.

Budget controls and monitoring are not optional — they are the difference between a productive autonomous system and an expensive runaway process.

Start small. Use `dry-run` mode. Set conservative budgets. Monitor actively until you trust the defaults. Then scale up.

---

*This advisory is based on forensic analysis of the complete Orchemist codebase as documented in the `docs/forensics/` audit series (01–08) and the Tier 4 documentation files.*
