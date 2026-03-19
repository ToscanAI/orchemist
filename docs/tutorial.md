# Your First Pipeline — A 10-Minute Tutorial

> **Goal:** Install Orchemist, run your first pipeline, and understand what happened.  
> **Time:** Under 10 minutes.  
> **You'll need:** A terminal and Python 3.10 or newer.

This tutorial uses the `hello-pipeline` template — a minimal, single-phase pipeline that accepts a message and echoes it back. No API key required for the first run (we start with dry-run mode).

Each step includes both a CLI path and an **IDE Alternative (MCP)** showing how to perform the same action from Claude Code or Cursor using natural language. For the IDE path, you'll need an Anthropic API key and a working MCP setup — see [docs/mcp-setup.md](mcp-setup.md) before starting.

For topics already covered in the [Getting Started guide](GETTING_STARTED.md) — Raspberry Pi setup, OpenClaw mode, advanced YAML authoring, and troubleshooting — this tutorial links out rather than repeating them.

---

## Prerequisites

**Python 3.10 or newer:**

```bash
python3 --version
# Should print Python 3.10.x or higher
```

If you're on Python 3.9 or older, see [Getting Started → Prerequisites](GETTING_STARTED.md#prerequisites) for upgrade options.

**For standalone (live) runs only:** an Anthropic API key. You don't need it for dry-run mode. When you're ready:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Add that to your `~/.bashrc` or `~/.zshrc` so it persists.

---

## Step 1: Install Orchemist

**Option A: Install from PyPI**

```bash
pip install orchemist
```

**Option B: Clone and install locally** *(recommended while the project is in active development)*

```bash
git clone https://github.com/ToscanAI/orchestration-engine.git
cd orchestration-engine
pip install -e .
```

The `-e` flag means "editable" — source changes take effect immediately without reinstalling.

**Verify the install:**

```bash
orch --help
```

You should see a help message listing available commands. If you see "command not found", your Python `bin` directory may not be on your `PATH`. Try:

```bash
python3 -m orchestration_engine --help
```

---

## Step 2: Scaffold a Pipeline Template

You can create a new pipeline template from scratch using:

```bash
orch new --yes --output templates/my-first-pipeline.yaml
```

This scaffolds a template with sensible defaults at the path you specify — no interactive prompts. The `--yes` flag accepts all defaults; omit it for an interactive wizard.

> **Cloned the repo?** You already have `templates/hello-pipeline.yaml`. Skip this step — we'll use that template directly.

**IDE Alternative (MCP):** Template scaffolding is a one-time CLI operation with no direct MCP equivalent. Run `orch new --yes --output templates/my-first-pipeline.yaml` from your terminal, then use the IDE for launching and monitoring. Once a template exists (including the built-in templates like `coding-pipeline-standard` and `content-pipeline-v28`), you can launch it from the IDE without any CLI scaffolding step.

---

## Step 3: Run in Dry-Run Mode

Dry-run mode validates your pipeline structure and runs all phases **without making any API calls**. It returns mock results immediately. Use it to confirm your YAML is wired up correctly before spending API credits.

```bash
orch run templates/hello-pipeline.yaml --mode dry-run
```

You should see output like this:

```
Example output (illustrative):
✓ Phase 'hello' completed (dry-run)

Pipeline completed.
Final output saved to: ./output/hello-pipeline-YYYYMMDD-HHMMSS/
```

If the command exits without error and reports a completed pipeline, dry-run mode is working.

**What dry-run mode does:** It validates your pipeline structure and passes mock data between phases — exactly what a real run would do, but without calling any AI model. Phases still execute and pass outputs to downstream phases; you're testing the plumbing, not the AI.

**IDE Alternative (MCP):** Dry-run mode has no direct MCP equivalent through `orchemist_launch`. The `orchemist_launch` tool always executes the pipeline for real (in standalone or openclaw mode). If you want to validate your pipeline YAML structure before spending API credits, use the CLI dry-run command above first:

```bash
orch run templates/hello-pipeline.yaml --mode dry-run
```

Once you've confirmed the structure is valid from the CLI, switch to the IDE for live runs.

---

## Step 4: Run Live (Standalone Mode)

Standalone mode executes your pipeline with **live API calls** to the Anthropic API. This requires a valid `ANTHROPIC_API_KEY` set in your environment.

```bash
orch run templates/hello-pipeline.yaml --mode standalone --input '{"message": "Hello from Orchemist!"}'
```

The `hello-pipeline` template accepts a single input field: `message`. The value you pass here gets forwarded into the phase prompt.

You should see output like this:

```
Example output (illustrative):
✓ Phase 'hello' completed (model: haiku-4-5, tokens: ~50, cost: ~$0.0001)

Pipeline completed in ~3s
Final output saved to: ./output/hello-pipeline-YYYYMMDD-HHMMSS/
```

**What standalone mode does:** It calls the Anthropic API for each phase and returns real model outputs. Unlike dry-run, this costs API credits and requires your key to be valid.

**IDE Alternative (MCP):** You can launch a live standalone pipeline directly from your IDE. After configuring MCP (see [docs/mcp-setup.md](mcp-setup.md)), type a natural language prompt into your IDE's chat — for example:

> Launch the coding pipeline in standalone mode. The repo is at /home/user/my-project, branch is feature/my-feature, issue title is "Add login timeout fix", issue number is 42, issue body: [paste body], and repo URL is https://github.com/myorg/my-project.

The IDE calls `orchemist_launch` with `template_id: "coding-pipeline-standard"` and `mode: "standalone"` automatically. You'll get a run ID back that you can use to check status.

---

## Step 5: Read Your Results

After a run completes, the output directory contains everything from the run:

```
./output/hello-pipeline-YYYYMMDD-HHMMSS/
├── phase_outputs/
│   └── hello.md          ← Output from the 'hello' phase
├── final_output.md        ← The last phase's result (what you usually want)
└── run_summary.md         ← Timing, cost, model used per phase
```

Open `final_output.md` to see the pipeline result. For the hello pipeline, it contains the model's response to your input message.

`run_summary.md` shows timing, token usage, and cost per phase — useful when tuning model tiers.

**IDE Alternative (MCP):** After launching a pipeline from the IDE, check its progress and read results using natural language:

> What is the status of run abc12345?

> Show me the logs for run abc12345.

> Show me the output from the write phase of run abc12345.

The IDE calls `orchemist_status` to get current phase, progress, elapsed time, and score — and `orchemist_logs` (with an optional phase name) to retrieve output content. You never need to open the output directory manually.

---

## What's Next

You've installed Orchemist and run your first pipeline. Here's where to go from here:

**Check run status and watch logs →** Once pipelines get longer, you'll want to monitor them while they run. See [docs/monitoring.md](monitoring.md) for the full monitoring command reference: `orch status`, `orch watch`, `orch logs`, and more.

**Launch a pipeline tied to a GitHub issue →** The `orch launch` command runs a pipeline and automatically fetches the GitHub issue as input:

```bash
orch launch templates/hello-pipeline.yaml --issue 42
```

This is the standard way to run issue-driven pipelines (the coding pipeline, content pipeline, etc.). It requires a `GITHUB_TOKEN` with read access to your repository.

**Write your own pipeline →** See [docs/GETTING_STARTED.md](GETTING_STARTED.md) for a full walkthrough of pipeline authoring: phases, `depends_on`, model tiers, and output forwarding. The [template authoring guide](template-authoring.md) covers the complete YAML schema.

---

## Command Summary

| Command / Tool | What it does |
|----------------|-------------|
| `orch new --yes --output <path>` | Scaffold a new pipeline template with defaults |
| `orch run <template> --mode dry-run` | Validate pipeline structure without API calls |
| `orch run <template> --mode standalone --input '{...}'` | Run pipeline with live API calls |
| `orch launch <template-id> --issue <n>` | Run pipeline tied to a GitHub issue |
| MCP: `orchemist_launch` | Launch a pipeline run by template ID from your IDE |
| MCP: `orchemist_status` | Get current status and progress of a pipeline run |
| MCP: `orchemist_logs` | Retrieve logs or phase output for a pipeline run |

For monitoring commands, see [docs/monitoring.md](monitoring.md).

For IDE setup and MCP configuration, see [docs/mcp-setup.md](mcp-setup.md).
