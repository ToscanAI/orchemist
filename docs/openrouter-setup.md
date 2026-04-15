# OpenRouter API Key Configuration

Configure `OPENROUTER_API_KEY` correctly before launching a pipeline with `--mode openrouter`. This guide covers the two supported configuration paths, how `orch serve` inherits environment variables, and how to recover from the `OpenRouter API key required` error without trial-and-error.

If you only ever run `--mode dry-run` or `--mode standalone` (Anthropic), you don't need an OpenRouter key. See [GETTING_STARTED.md](GETTING_STARTED.md) for the full mode overview.

---

## When you need this

OpenRouter is a multi-provider router — one API key, many backing models (Anthropic, OpenAI, Mistral, etc.). You need `OPENROUTER_API_KEY` if you run any pipeline with:

```bash
orch run my-pipeline.yaml --mode openrouter ...
```

Get a key at [openrouter.ai/keys](https://openrouter.ai/keys). Keys look like `sk-or-v1-...`.

---

## Available modes and the keys they require

The `orch run` CLI accepts four modes. Only `openrouter` uses `OPENROUTER_API_KEY`:

| Mode          | Required credential                     | Notes                                                 |
|---------------|-----------------------------------------|-------------------------------------------------------|
| `standalone`  | `ANTHROPIC_API_KEY` (or `--api-key`)    | Direct Anthropic API. Default mode.                   |
| `openrouter`  | `OPENROUTER_API_KEY` (or `--api-key`)   | Multi-provider routing via OpenRouter.                |
| `openclaw`    | `OPENCLAW_GATEWAY_TOKEN` + gateway URL  | Sub-agent execution via the OpenClaw gateway.         |
| `dry-run`     | None                                    | Mock executor — no network calls, no keys needed.     |

Mode names are validated by the CLI; values outside this set are rejected by `orch run --mode`.

---

## Configuration options

Two paths are supported. If both are provided, the CLI flag wins.

### Option 1 — CLI flag (per-invocation)

```bash
orch run my-pipeline.yaml --mode openrouter --api-key sk-or-v1-...
```

The `--api-key` flag on `orch run` is reused for both `standalone` and `openrouter` modes — the runner picks the right environment variable based on `--mode`.

Good for: one-off runs, CI jobs where the key comes from a secret manager, sharing a terminal with someone who has a different key in their shell.

### Option 2 — Environment variable (persistent)

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
orch run my-pipeline.yaml --mode openrouter
```

To make it persist across shell sessions, add the `export` line to `~/.bashrc`, `~/.zshrc`, or your shell's equivalent, then open a new terminal.

### Precedence

1. `--api-key` flag value (if passed)
2. `OPENROUTER_API_KEY` from the process environment
3. Otherwise: the runner raises `ValueError` (see [Troubleshooting](#troubleshooting))

The relevant resolution is in [`src/orchestration_engine/pipeline_runner.py`](../src/orchestration_engine/pipeline_runner.py):

```python
resolved_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
```

---

## `orch serve` and environment inheritance

`orch serve` launches the local web UI + REST API. When you trigger pipeline runs from the UI, they execute **inside the `orch serve` process** — so they see only the environment variables that were exported in the shell that started `orch serve`.

**Correct:**

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
orch serve
# Now open http://127.0.0.1:8374 and trigger runs with mode=openrouter
```

**Incorrect — this does NOT work:**

```bash
# Terminal A
orch serve        # started without the key exported

# Terminal B (separate shell)
export OPENROUTER_API_KEY=sk-or-v1-...   # this export only exists in Terminal B
```

The `export` in Terminal B does not reach the process running in Terminal A. The UI run will fail with `OpenRouter API key required`.

### Restart behavior

A running `orch serve` process captures its environment at startup. **Exporting a new value into the parent shell after `orch serve` is already running has no effect** — the child process keeps the snapshot it had at launch.

If you realize the key is missing (or needs rotating) after `orch serve` has started:

```bash
# Stop the running server (Ctrl-C in the terminal it's running in)
export OPENROUTER_API_KEY=sk-or-v1-...
orch serve
```

Note: `orch serve` itself does not accept an `--api-key` flag. The environment variable is the only way to configure it.

---

## IDE integrated terminals

Setting the key in the **integrated terminal of the Orchemist IDE** (or VS Code / Cursor / Claude Code) does **not** propagate to the Orchemist engine process if the engine was launched outside that terminal (for example, by the IDE's built-in launcher or a background systemd unit).

Concretely:

- If the engine is started by the IDE's launcher, the IDE — not the integrated terminal — is the parent process. Env vars exported in the integrated terminal are scoped to that shell and won't reach the engine.
- To export a key that the engine can see, set it in the environment that starts the engine (the IDE's own launch configuration, a systemd unit, or the shell you manually run `orch serve` from).

See the IDE-side tracking issue for the UX counterpart: [orchemist-ide#41](https://github.com/ToscanAI/orchemist-ide/issues/41).

---

## `.env` file support

`.env` / `~/.orchestration-engine/.env` loading is **not currently implemented**. The repo ships an `.env.example` for reference, but no code path calls `dotenv.load_dotenv()` — variables listed in a local `.env` will **not** be picked up automatically by `orch run` or `orch serve`.

If you want `.env`-style behavior today, source the file manually before running:

```bash
set -a
source .env
set +a
orch serve
```

First-class `.env` loading is a candidate future improvement; track progress on the repository issue tracker.

---

## Troubleshooting

### Error: `OpenRouter API key required`

When the runner cannot find a key, it raises this exact message (from `src/orchestration_engine/pipeline_runner.py:209-211`):

```
OpenRouter API key required.
  Option 1: orch run --api-key sk-or-...
  Option 2: export OPENROUTER_API_KEY=sk-or-...
```

Map to a fix:

| What you see / did                                              | Likely cause                                                                      | Fix                                                                                      |
|-----------------------------------------------------------------|-----------------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| Ran `orch run ... --mode openrouter` with nothing exported      | No key in environment and no `--api-key` flag                                     | Pass `--api-key sk-or-v1-...` or `export OPENROUTER_API_KEY=...` before running.         |
| `echo $OPENROUTER_API_KEY` prints the key but the run still fails | `orch serve` was started before you exported the key                             | Stop `orch serve`, re-export the key in the same shell, relaunch `orch serve`.           |
| Triggered a run from the web UI                                 | Web UI uses the `orch serve` process env — same restart rule applies              | Restart `orch serve` with the key exported in its parent shell.                          |
| Exported the key in the IDE integrated terminal                 | Engine process is a different child of the IDE (or a separate systemd unit)       | Set the key in the IDE's launch config / systemd unit, not in the integrated terminal.   |
| Put the key in `.env`                                           | `.env` is not auto-loaded                                                         | Source the file manually (`set -a; source .env; set +a`) or export the variable.          |
| Both `--api-key` and env var set, but wrong key is used         | `--api-key` flag takes precedence                                                 | Drop the flag, or pass the correct value via `--api-key`.                                |

### Quick sanity check

```bash
# Confirm the variable is visible in the shell that will run the engine
echo "${OPENROUTER_API_KEY:-<unset>}"

# Confirm dry-run works (no key needed) — isolates a key problem from a pipeline problem
orch run my-pipeline.yaml --mode dry-run --input '{"topic": "test"}'

# Confirm an openrouter run end-to-end
orch run my-pipeline.yaml --mode openrouter --api-key sk-or-v1-... \
  --input '{"topic": "test"}'
```

If the dry-run passes but `--mode openrouter` fails with the error above, the problem is in how the key is being delivered to the engine process — revisit the env-inheritance notes above.

---

## See also

- [GETTING_STARTED.md](GETTING_STARTED.md) — first-pipeline walkthrough
- [mcp-setup.md](mcp-setup.md) — API keys for IDE / MCP integrations
- [orchemist-ide#41](https://github.com/ToscanAI/orchemist-ide/issues/41) — IDE-side integrated-terminal UX
