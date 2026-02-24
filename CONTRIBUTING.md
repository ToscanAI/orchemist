# Contributing to Orchestration Engine

Welcome! We're glad you're here. 🎉

The most impactful way to contribute is by **sharing a great pipeline template** — a reusable YAML workflow that others can copy, run, and build on. You don't need to understand the engine internals to do it. If you've built a pipeline that works well, it belongs in this library.

Code contributions are also very welcome, and we've got a section for those too. But if you're here for templates, jump straight in.

---

## 1. Your First Template in 5 Minutes

The fastest path to contribution is cloning an existing example, tweaking it, and opening a PR.

```bash
# 1. Fork and clone the repo
git clone https://github.com/<your-username>/orchestration-engine.git
cd orchestration-engine

# 2. Install the engine so you can test locally
pip install -e .

# 3. Copy a starter template
cp examples/hello-pipeline.yaml examples/my-pipeline.yaml

# 4. Edit it to do something useful (see Template Structure below)
#    Name your file descriptively: summarize-podcast.yaml, code-review-pr.yaml, etc.

# 5. Validate and do a dry run
orch validate examples/my-pipeline.yaml
orch run examples/my-pipeline.yaml --mode dry-run --input '{"topic": "test"}'

# 6. Commit and open a PR
git checkout -b template/my-pipeline
git add examples/my-pipeline.yaml
git commit -m "feat(templates): add my-pipeline for X use case"
git push origin template/my-pipeline
```

That's it. If the dry run passes and the template does something genuinely useful, it has a good chance of being merged.

---

## 2. Template Structure

Every template is a single YAML file. Here's the full structure with annotations:

```yaml
# ── Identity ──────────────────────────────────────────────────────────────────
id: content-pipeline-v2          # Unique slug, lowercase-kebab. Required.
name: "Content Creation Pipeline v2"   # Human-readable title. Required.
version: "1.0.0"                 # Semver. Bump when you change behavior.
description: >
  A 7-phase pipeline that takes a topic from research through drafting,
  parallel review, and iterative refinement to a publication-ready article.
author: "Your Name"              # Your GitHub handle or name. Optional but nice.
category: "content"              # content | code | research | data | other

tags:                            # Freeform. Helps orch templates search find your work.
  - content
  - writing
  - research

use_cases:                       # 2-4 bullet points. Help users decide if this fits them.
  - "Long-form blog posts with adversarial review"
  - "LinkedIn articles with multi-perspective quality checks"

example_input:                   # A minimal working input. Used in docs and --dry-run.
  topic: "The impact of LLMs on software development"
  tone: "professional"
  word_count: 1500

# ── Config Schema ─────────────────────────────────────────────────────────────
# Defines what inputs the pipeline accepts. Used for validation and the
# interactive `orch start` wizard. JSON Schema format.
config_schema:
  type: object
  properties:
    topic:
      type: string
      description: "Article topic or brief"
    tone:
      type: string
      default: "professional"
      description: "Writing tone, e.g. 'professional', 'conversational'"
    word_count:
      type: integer
      default: 1500
      description: "Target word count for the draft"
  required:
    - topic              # Only truly required fields go here

# ── Phases ────────────────────────────────────────────────────────────────────
# Each phase is one AI call. Phases run after their depends_on list completes.
# Phases with no depends_on run first (potentially in parallel).
phases:

  - id: research               # Unique within this template. Referenced in depends_on.
    name: "Source Research"
    description: "Gather key facts, sources, and expert perspectives."
    task_type: research        # research | content | review | code | translation
    model_tier: sonnet         # haiku (fast/cheap) | sonnet (balanced) | opus (best)
    thinking_level: low        # off | low | medium | high — controls extended thinking
    depends_on: []             # Empty = runs immediately
    timeout_minutes: 30
    prompt_template: |
      Research the following topic and produce a structured brief with:
      1. 5–8 key facts with source references
      2. Notable expert perspectives
      3. Recent trends and developments

      Topic: {input[topic]}
      Target tone: {input[tone]}

  - id: draft
    name: "Full Draft"
    description: "Write a complete article from the research brief."
    task_type: content
    model_tier: sonnet
    thinking_level: medium
    depends_on:
      - research               # Waits for research; gets its output via previous_output
    timeout_minutes: 45
    prompt_template: |
      Write a complete {input[word_count]}-word article in a {input[tone]} tone.

      === RESEARCH BRIEF ===
      {previous_output[research]}     # ← Output of the research phase, injected here

      Produce only the article text — no meta-commentary.
```

### Key interpolation variables

| Variable | What it resolves to |
|---|---|
| `{input[key]}` | A value from the user's `--input` JSON |
| `{previous_output[phase-id]}` | The text output of another phase |

### Model tier guide

| Tier | Best for | Cost |
|---|---|---|
| `haiku` | Classification, first-pass research, cheap bulk tasks | 💚 Very low |
| `sonnet` | Production writing, code generation, review | 💛 Medium |
| `opus` | Complex reasoning, integration, critical decisions | 🔴 High |

---

## 3. Local Testing Workflow

Always test locally before opening a PR. Three commands cover everything:

**Step 1 — Validate structure**
```bash
orch validate examples/my-pipeline.yaml
```
Catches YAML syntax errors, missing required fields, and invalid `depends_on` references. Use `--fix` to auto-correct simple issues:
```bash
orch validate examples/my-pipeline.yaml --fix
```

**Step 2 — Dry run (no API key needed)**
```bash
orch run examples/my-pipeline.yaml \
  --mode dry-run \
  --input '{"topic": "quantum computing", "tone": "professional"}'
```
The dry-run executor returns mock results immediately. This tests your YAML structure, variable interpolation, and phase dependency graph without calling any API. Use it freely — it's free and instant.

**Step 3 — Real run (requires API key)**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
orch run examples/my-pipeline.yaml \
  --mode standalone \
  --input '{"topic": "quantum computing", "tone": "professional"}'
```
Run your pipeline end-to-end against Claude at least once before submitting. Make sure the output is actually good. A template that runs but produces mediocre output won't get merged.

**Preview the execution plan**
```bash
orch list-phases examples/my-pipeline.yaml
```
Shows phase order, model tiers, and which phases run in parallel. Handy for debugging dependency graphs.

---

## 4. Publishing Your Template

Once you're happy with local testing, publish it:

```bash
# Push your template to your fork, then publish to the community index:
## Publishing is done by opening a PR — add your template to `examples/` and submit.
```

This registers your template in the community index so others can install it with:
```bash
orch templates install your-github-username/orchestration-engine
```

Or from a specific path:
```bash
orch templates install ./examples/my-pipeline.yaml --name my-pipeline
```

For inclusion in the **official** template library (bundled with the engine), open a PR to `templates/` in this repo. PRs to `examples/` are also welcome — those become the reference examples shown in docs and quickstart.

---

## 5. Code Contributions

If you want to work on the engine itself (executors, CLI, scenario runner, etc.), here's how to set up a full dev environment:

```bash
git clone https://github.com/<your-username>/orchestration-engine.git
cd orchestration-engine
python3 -m venv .venv && source .venv/bin/activate

# Install with all dev and web extras
pip install -e ".[dev,web]"
```

**Run the test suite**
```bash
pytest                          # All tests
pytest tests/ -q                # Quiet mode
pytest tests/test_templates.py  # One file
pytest -k "dry_run"             # Tests matching a keyword
```

All tests must pass before you open a PR. The CI checks this automatically.

**PR process**
1. Fork the repo and create a feature branch: `git checkout -b feat/my-feature`
2. Make your changes
3. Run `pytest` — all tests must pass
4. Run `orch validate` on any templates you touched
5. Open a PR with a clear description: what it does, why it's useful, any trade-offs

For larger changes (new executors, architecture changes), open an issue first to discuss the approach before writing code. Saves everyone time.

---

## 6. Code Style

**Python conventions**
- Follow [PEP 8](https://peps.python.org/pep-0008/) — 4-space indents, 100-char line limit (Black-compatible)
- Type hints on all public functions and class methods
- Docstrings on all public classes and methods (Google style)
- Prefer explicit over clever — this codebase runs in production and needs to be readable

**Running linters**
```bash
black .           # Auto-format
flake8 .          # Lint
mypy src/         # Type check
```

**Commit message format**

Use the standard prefixes — they keep the changelog readable:

```
feat(templates): add podcast-summarizer template
fix(executor): handle timeout on dry-run mode
docs: improve GETTING_STARTED quickstart section
test: add scenario tests for content-pipeline
refactor(phases): extract dependency resolver into own module
chore: bump anthropic SDK to 0.28
```

Format: `<prefix>(<scope>): <short description in present tense>`

Keep the subject line under 72 characters. Add a body if the change needs explanation. Reference issues with `Closes #123` in the body.

---

## 7. Issue & PR Guidelines

### Filing a bug report

Include all of these — without them we can't reproduce the problem:
- **What you ran:** the exact `orch` command, your template file, and your `--input`
- **What you expected**
- **What actually happened** (paste the full error output)
- **Environment:** OS, Python version (`python3 --version`), engine version (`orch --version`)

```markdown
## Bug: Phase output not forwarded when depends_on list has 3+ entries

**Command:** `orch run my-pipeline.yaml --mode standalone --input '{"topic": "test"}'`
**Expected:** apply-fixes phase receives output from all three review phases
**Actual:** `{previous_output[red-team]}` resolves to empty string
**Environment:** macOS 14.2, Python 3.11.6, orch 0.4.1
```

### Submitting a template PR

Your PR description should include:
- **What the template does** in 2–3 sentences
- **A working example input** and the actual output it produced
- **Why someone would use this** over existing templates
- **Which AI calls it makes** (phases, model tiers, approximate cost per run)

We won't merge a template we haven't seen run successfully. Paste a sample output in the PR description — it saves review time and builds trust.

### Feature requests

Open an issue with the `enhancement` label. Describe the use case, not the implementation. "I need X because I'm trying to do Y" is much more useful than "please add Z flag".

---

## 8. License

Orchestration Engine is released under the **MIT License**.

By contributing — whether a template, code fix, or documentation improvement — you agree that your contribution is submitted under the same MIT license. You retain copyright on your work; you're just granting everyone (including the project) the right to use, modify, and distribute it under MIT terms.

See [LICENSE](LICENSE) for the full text.

---

**Questions?** Open an issue, or drop a question in the PR. We're friendly, we read everything, and we try to respond within a day or two.

Happy building. 🚀
