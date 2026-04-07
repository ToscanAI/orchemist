# Orchestration Engine — Web UI Getting Started

## Quick Start (30 seconds)

```bash
cd ~/orchestration-engine
git checkout feature/744-Self-Sufficient-Web-UI-with-Multi-Model-Routing
pip install -e ".[web]" --break-system-packages
orch serve
```

Browser opens automatically to **http://localhost:8374**

---

## What You'll See

### 1. Dashboard (Home Page)
Shows all available pipeline templates as cards. Click any template to open its detail page.

### 2. Template Detail (`/templates/{id}`)
- **Phase Execution Plan** — shows each phase, its model tier, and thinking level
- **Launch Form** — this is where you run pipelines from the browser:
  - **Mode selector**: `dry-run` | `standalone` | `openclaw` | `openrouter`
  - **Config fields**: auto-generated from the template's `config_schema` (text fields, dropdowns, checkboxes). Falls back to raw JSON if no schema defined.
  - **Load Example** button: pre-fills from the template's example input
  - **Provider credentials**: appears for `standalone` and `openrouter` modes
  - **Phase Model Map**: shows model assigned to each phase with override dropdowns

### 3. Runs List (`/runs`)
- Filterable by **status** (all, pending, running, success, failed, cancelled, crashed)
- Filterable by **template name**
- Paginated (20 per page), auto-refreshes every 10 seconds
- Click any run to drill into its detail page

### 4. Run Detail (`/runs/{id}`)
- **Timeline tab**: live-updating phase progress via SSE (server-sent events)
- **Logs tab**: daemon log viewer with auto-scroll and refresh
- **Cancel button**: visible for running/pending runs (red button in header)
- **Observer panel**: click 🔍 Observer to expand — shows heuristic alerts:
  - Slow phases (>2x the tier average)
  - High token usage per model tier
  - Cost milestones ($1, $3)
  - Phase failures
  - Terminal run summary

---

## Running a Test Pipeline

### Option A: Dry Run (no API key needed)
1. Open http://localhost:8374
2. Click any template (e.g. "Content Pipeline")
3. Select mode: **dry-run**
4. Fill in the input fields or click **Load Example**
5. Click **Launch Run**
6. You'll be redirected to the run detail page — watch phases complete in real-time

### Option B: Standalone (Anthropic direct)
1. Same as above but select mode: **standalone**
2. Enter your **Anthropic API key** in the credential field (or leave blank if `ANTHROPIC_API_KEY` is set in your environment)
3. Click **Launch Run**

### Option C: OpenRouter (multi-provider)
1. Same flow, select mode: **openrouter**
2. Enter your **OpenRouter API key** (get one at https://openrouter.ai/keys)
3. Optionally override phase models in the **Phase Model Map** table
4. Click **Launch Run**

---

## OpenRouter Setup

### Do I need to install anything extra?
**No.** OpenRouter support is built into the orchestration engine. The `httpx` library (already installed with `[web]` extras) handles the API calls.

### What is OpenRouter?
A unified API gateway that routes to 200+ models (OpenAI, Anthropic, Google, Meta, Mistral, DeepSeek, etc.) through a single API key and endpoint. You pay per token based on the model you use.

### Getting an API Key
1. Go to https://openrouter.ai
2. Create an account
3. Go to https://openrouter.ai/keys → **Create Key**
4. Add credit ($5 minimum) at https://openrouter.ai/credits
5. Copy the key (starts with `sk-or-...`)

### Using OpenRouter from the CLI
```bash
# Set the API key
export OPENROUTER_API_KEY="sk-or-v1-your-key-here"

# Run a pipeline
orch run content-pipeline --mode openrouter --input '{"brief": "AI agents overview"}'

# With custom model mapping (route sonnet-tier phases to GPT-4o)
orch run content-pipeline --mode openrouter \
  --model-map '{"sonnet": "openai/gpt-4o", "opus": "anthropic/claude-opus-4-6"}'

# Launch (non-blocking, daemon-based)
orch launch content-pipeline --mode openrouter --input-file /tmp/input.json
```

### Using OpenRouter from the Web UI
1. Select mode: **openrouter**
2. Paste your API key in the credential field
3. Use the Phase Model Map to override models per tier:
   - Default: Anthropic Claude models (haiku/sonnet/opus)
   - Available overrides: GPT-4o, GPT-4o Mini, Gemini 2.5 Pro, DeepSeek R1
4. Launch — the key is passed securely to the daemon process (never persisted to DB)

### Default Model Mapping
| Tier | Default Model |
|------|---------------|
| haiku | `anthropic/claude-haiku-4-5-20251001` |
| sonnet | `anthropic/claude-sonnet-4-6` |
| opus | `anthropic/claude-opus-4-6` |

You can override any tier to any model available on OpenRouter.

---

## Merge Gates

After a pipeline completes, it may create a **merge gate** requiring your approval before the branch merges.

### From the CLI
```bash
orch gate list                     # Show all pending gates
orch gate info <run-id>           # Gate details
orch gate approve <run-id>        # Approve merge
orch gate reject <run-id> -m "needs fixes"
```

### From the API
- `GET /api/v1/gates` — list all gates (filterable by status)
- `GET /api/v1/gates/{run_id}` — gate detail
- `POST /api/v1/gates/{run_id}/approve` — approve (with optional `force: true` for failed scoring)
- `POST /api/v1/gates/{run_id}/reject` — reject with reason

---

## Useful Commands

```bash
# Start the unified server
orch serve                        # http://localhost:8374
orch serve --port 9000           # custom port
orch serve --no-open             # don't auto-open browser

# Check a run
orch status <run-id>
orch logs <run-id>

# API docs
# Open http://localhost:8374/api/v1/docs (Swagger UI)
```

---

## Troubleshooting

**"Frontend not built" warning?**
```bash
cd frontend && npm run build && cd ..
orch serve
```

**Port already in use?**
```bash
orch serve --port 9000
# or kill the existing process:
lsof -ti:8374 | xargs kill
```

**OpenRouter returns 401?**
Your API key is invalid or expired. Check at https://openrouter.ai/keys

**OpenRouter returns 402?**
Insufficient credits. Add funds at https://openrouter.ai/credits
