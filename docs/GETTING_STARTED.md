# Getting Started — From Zero to First Pipeline

> **You'll have a working pipeline in under 5 minutes.** This guide assumes you know what a terminal is and can run a Python command. That's it.

---

## Prerequisites

Before you start, make sure you have:

**Python 3.10 or newer**

Check with:
```bash
python3 --version
# Should print Python 3.10.x or higher
```

If you're on Python 3.9 or older, install a newer version from [python.org](https://python.org) or use `pyenv`.

**An API key — one of these:**

- **Anthropic API key** — go to [console.anthropic.com](https://console.anthropic.com), create an account, and grab a key. Set it as an environment variable:
  ```bash
  export ANTHROPIC_API_KEY="sk-ant-..."
  ```
  Add that line to your `~/.bashrc` or `~/.zshrc` so it persists across sessions.

- **OpenClaw** — if you're running inside OpenClaw, you're already set. The executor will use OpenClaw's session infrastructure automatically.

---

## Install

**Option A: Install from PyPI** *(when the package is published)*
```bash
pip install orchestration-engine
```

**Option B: Clone and install locally** *(recommended while the project is in active development)*
```bash
git clone https://github.com/ToscanRivera/orchestration-engine.git
cd orchestration-engine
pip install -e .
```

The `-e` flag means "editable" — changes you make to the source are reflected immediately without reinstalling.

Verify the install worked:
```bash
orch --help
```

You should see a help message listing available commands. If you see "command not found", your Python `bin` directory might not be on your `PATH`. Try:
```bash
python3 -m orchestration_engine --help
```

---

## Your First Pipeline

The fastest way to get started:

```bash
# Option 1: Copy a working example to your current directory
orch quickstart

# Option 2: Interactive wizard — answer questions, get a custom pipeline
orch start

# Option 3: Browse available templates
orch templates list
orch templates info content-pipeline
```

Or build one from scratch. Let's create a simple two-phase pipeline: one phase to research a topic, one to write a short summary.

Create a file called `my-pipeline.yaml` anywhere on your machine:

```yaml
id: my-first-pipeline
name: "My First Pipeline"
version: "1.0.0"
description: "Research a topic, then write a short summary."

config_schema:
  type: object
  properties:
    topic:
      type: string
      description: "What to research and summarize"

phases:
  - id: research
    name: "Research"
    task_type: research
    model_tier: haiku          # Fast and cheap — good for research
    thinking_level: low
    depends_on: []
    timeout_minutes: 15
    prompt_template: |
      Research the following topic and produce a brief with:
      - 5 key facts
      - 2-3 credible sources

      Topic: {input[topic]}

  - id: write
    name: "Write Summary"
    task_type: content
    model_tier: sonnet          # Better writing quality
    thinking_level: low
    depends_on: [research]      # Wait for research to finish first
    timeout_minutes: 15
    prompt_template: |
      Write a clear, 200-word summary based on this research:

      {previous_output[research]}

      The summary should be suitable for a general audience.
```

A few things to notice:
- `research` has `depends_on: []` — it runs first, with no prerequisites
- `write` has `depends_on: [research]` — it waits for `research` and gets access to its output via `{previous_output[research]}`
- Each phase picks its own model tier — Haiku for research (cheap, fast), Sonnet for writing (better quality)

---

## Run It

```bash
orch run my-pipeline.yaml --input '{"topic": "the history of the Eiffel Tower"}'
```

Or if you prefer a JSON file for the input:

```bash
# Create input.json
echo '{"topic": "the history of the Eiffel Tower"}' > input.json

# Run with file input
orch run my-pipeline.yaml --input-file input.json
```

**Dry run mode** — run the pipeline without actually calling the AI (useful for testing your YAML structure):
```bash
orch run my-pipeline.yaml --input '{"topic": "test"}' --mode dry-run
```

In dry-run mode, the executor returns mock results immediately. Your phases still run and pass data between each other — you're just testing the plumbing, not the AI.

---

## Check Results

When the pipeline finishes, you'll see output like this in your terminal:

```
✓ Phase 'research' completed (model: haiku-4-5, tokens: 847, cost: $0.0003)
✓ Phase 'write' completed    (model: sonnet-4,  tokens: 1243, cost: $0.0024)

Pipeline completed in 18.4s
Final output saved to: ~/.orchestration-engine/runs/my-first-pipeline-20260220-143205/

Phase outputs:
  research → 5 key facts about the Eiffel Tower construction...
  write    → The Eiffel Tower, completed in 1889 as the entrance arch...
```

The output directory contains:
```
~/.orchestration-engine/runs/my-first-pipeline-YYYYMMDD-HHMMSS/
├── phase_outputs/
│   ├── research.json    ← Full research result
│   └── write.json       ← Full writing result
├── final_output.json    ← The last phase's result (what you probably want)
└── run_summary.json     ← Timing, cost, model used per phase
```

Open `final_output.json` to see the complete result. The `result` field contains the actual content.

**Checking history from the database:**
```bash
orch status                  # Show recent pipeline runs
orch list                    # List all tasks with their states
orch status <task-id>         # Show details for a specific task
```

---

## Next Steps

Once your first pipeline is working, here's where to go next:

### Add More Phases
The content pipeline template at `templates/content-pipeline.yaml` shows a full 5-phase example: research → write → fact-check → apply fixes → final output. Study it to see how phases chain together and how outputs accumulate.

### Write Scenarios (Acceptance Tests)
After your pipeline runs, you want to verify the output is actually good. Create a `scenarios/` directory next to your template:

```yaml
# scenarios/happy-path.yaml
id: happy-path
acceptance:
  - id: summary-exists
    type: assertion
    check: "len(output.get('result', '')) > 100"
    weight: 0     # gate: must pass

  - id: quality-check
    type: llm_judge
    rubric: |
      Rate the following summary on a scale of 0.0 to 1.0.
      Consider: clarity, accuracy, and readability.
      End your response with exactly: Score: X.X
    judge_model: claude-haiku-4-5-20241022
    weight: 1
    threshold: 0.6

scoring:
  pass_threshold: 0.6
  gate_mode: all_or_nothing
```

Run a scenario:
```bash
orch scenario run scenarios/happy-path.yaml
```

### Custom Graders
The assertion and LLM judge graders cover most cases. If you need something specific — say, checking a word count range or validating JSON structure — write a custom grader by subclassing the grader base class. See `scenario_runner/graders/` for examples.

### Parallel Pipelines
Phases that don't depend on each other can theoretically run in parallel (the dependency graph groups them into independent "waves"). The current version runs them serially for simplicity, but the structure is ready for concurrency when you need it.

### Model Tuning
- Use `haiku` for cheap, repetitive tasks (translation, classification, first-pass research)
- Use `sonnet` for production writing and code generation
- Use `opus` only when quality is critical and cost doesn't matter as much
- Set `thinking_level: high` for phases that need deep reasoning (complex debugging, nuanced editorial decisions)

---

## Running on a Raspberry Pi

Yes, this runs on a Raspberry Pi. Here's what you need and what to watch out for.

**Tested on:** Raspberry Pi 4 (4GB RAM), Raspberry Pi 5. The Pi Zero is too limited — skip it.

**Setup:**
```bash
# Update and install Python (Raspberry Pi OS comes with Python 3.11+)
sudo apt update && sudo apt install -y python3-pip python3-venv

# Create a virtual environment (good practice)
python3 -m venv ~/orch-env
source ~/orch-env/bin/activate

# Install the engine
git clone https://github.com/ToscanRivera/orchestration-engine.git
cd orchestration-engine
pip install -e .
```

**Things to know:**

- **The Pi doesn't run the AI models** — it just sends HTTP requests to the Anthropic API. The heavy lifting happens in the cloud. A Pi 4 with 4GB RAM is more than enough for the engine itself.
- **SQLite is fine** — the database is tiny. Even 1,000 pipeline runs won't stress a Pi's SD card.
- **Network is the bottleneck** — if your Pi is on slow WiFi, API calls will take longer. Use Ethernet if you care about latency.
- **Set your API key properly** — don't forget to add `export ANTHROPIC_API_KEY="sk-ant-..."` to `~/.bashrc` so it survives reboots.
- **Run as a service** — to run pipelines on a schedule or always-on, set up a systemd service:

```ini
# /etc/systemd/system/orch-engine.service
[Unit]
Description=Orchestration Engine
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/orchestration-engine
Environment="ANTHROPIC_API_KEY=sk-ant-..."
ExecStart=/home/pi/orch-env/bin/orch serve --host 0.0.0.0 --port 8080
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable orch-engine
sudo systemctl start orch-engine
```

- **Memory:** The engine itself uses ~50–80MB RAM. You're fine on a Pi 4 with 4GB. On a Pi 4 with 2GB, close browser tabs and other services if you're running many concurrent pipelines.
- **SD card longevity:** SQLite writes frequently. If you're running pipelines 24/7, consider mounting the database on a USB SSD rather than the SD card:
  ```bash
  orch serve --host 0.0.0.0 --port 8080 --db-path /mnt/usb/engine.db
  ```

The Pi is a great choice for a local automation box that runs pipelines on a schedule — say, generating a weekly content digest or monitoring a topic and summarizing updates overnight.

---

## Troubleshooting

**"No executor available for task type..."**
Make sure you're not in dry-run mode when you want real execution, or vice versa. Check your config.

**"Phase 'X' failed, aborting pipeline."**
Look at the error output — it'll say which executor failed and why. Common causes: missing API key, network timeout, or the model returned an unexpected format. Try running with `--dry-run` first to rule out YAML issues.

**"Template missing required field 'id'"**
Your YAML file is missing a top-level `id:` field. Every template needs one.

**"Cycle detected involving phase(s): ..."**
Two of your phases depend on each other (A depends on B, B depends on A). That's a circular dependency — break the cycle by removing one dependency.

**Database errors**
The database lives at `~/.orchestration-engine/engine.db`. If something is badly corrupted, you can delete it and start fresh:
```bash
rm ~/.orchestration-engine/engine.db
```
(You'll lose task history but nothing else.)
