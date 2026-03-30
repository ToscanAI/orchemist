# Open Source Launch Requirements

**Date:** 2026-03-13
**Status:** Pre-launch assessment
**Scope:** What must be done before Orchemist goes public on GitHub and PyPI

---

## Current State

| Metric | Count |
|---|---|
| Source modules | 52 Python files |
| Engine code | ~33,500 lines |
| Test code | ~70,000 lines (129 files) |
| Test-to-code ratio | 2:1 |
| Documentation | ~14,500 lines (42 markdown docs) |
| Templates (bundled) | 9 |
| Templates (examples) | 11 |
| Scenarios | 9 YAML test suites |
| Version | 0.3.0 (Alpha) |

**Verdict:** The engine core is built. The code is ahead of the launch. Ship what exists.

---

## 1. Must-Do Before Launch (Days, Not Weeks)

### 1.1 Publish to PyPI

The README says `pip install orchemist`. It doesn't work. First-impression users will try it and get nothing.

- [ ] Register `orchemist` on PyPI
- [ ] Test `pip install orchemist` in a clean virtualenv
- [ ] Verify `orch --help` works after install
- [ ] Verify `orch run templates/hello-pipeline.yaml --mode dry-run` works out of the box

### 1.2 Add GitHub Actions CI

The README badge says "tests passing" but there is no visible CI configuration. 3,000+ tests mean nothing to an outsider without green CI.

- [ ] Create `.github/workflows/ci.yml` — run `pytest` on push/PR
- [ ] Target Python 3.10, 3.11, 3.12
- [ ] Include `orch validate` on all bundled templates
- [ ] Make the badge link to the actual workflow

### 1.3 Run Every Template End-to-End

The forensics audit found that the sole content pipeline (v27) had broken file-handoff and the research template was non-functional with bare `{var}` placeholders. Both were fixed in v28 and v2 respectively, but they need a real run to confirm.

- [ ] `orch run` every bundled template in `--mode dry-run` — all must pass
- [ ] `orch run` every example template in `--mode dry-run` — all must pass
- [ ] `orch validate` every YAML file — zero errors
- [ ] Run at least `content-pipeline-v28`, `research-competitive-v2`, and `coding-pipeline-v1` in `--mode standalone` with real API calls

### 1.4 Write a "Your First Pipeline" Tutorial

The README quickstart is close but needs a working example that produces visible output.

- [ ] Create `docs/tutorial.md` — 5-minute walkthrough
- [ ] Start with `pip install orchemist`
- [ ] Show `orch new --yes` creating a template
- [ ] Show `orch run` in dry-run mode with output
- [ ] Show `orch run` in standalone mode with a real API key
- [ ] End with "next steps" pointing to the template authoring guide

### 1.5 Clarify the OpenClaw Dependency

The OpenClaw executor is the primary execution mode but the gateway is not publicly available. New users will be confused.

- [ ] Add a clear section to the README: "Orchemist works in three modes: standalone (Anthropic API), openclaw (sub-agent gateway), dry-run (testing). You only need an Anthropic API key to get started."
- [ ] Mark OpenClaw mode as "requires separate gateway setup" in the CLI help
- [ ] Ensure standalone mode is the default or clearly documented first-choice

---

## 2. Do NOT Do Before Launch

These are real features but they cost weeks-to-months and will delay launch indefinitely. Ship first, build later.

| Item | Why not now |
|---|---|
| Visual pipeline builder (1.1) | Months of frontend work. No users are asking for it yet. |
| Monaco editor (1.2) | Luxury feature. YAML editing in VS Code works fine. |
| Template marketplace (1.4) | Needs users before it needs a marketplace. |
| Meta-orchestration (4.2) | Pipeline-of-pipelines is a Level 5 feature. |
| Deployment integration (4.3) | Premature without users running real workloads. |
| Go rewrite (`orchemist-v2`) | A trap. Do not split energy. Ship Python. Rewrite only if you hit real performance walls with users. |

---

## 3. What Makes Orchemist Worth Something

### Genuine differentiators

1. **Self-hosted: Orchemist built itself** — The coding pipeline (`coding-pipeline-v1`) was used to incrementally build Orchemist's own features. Every major capability — error recovery, model fallback, cost tracking, trust calibration — was developed through the tool's own pipeline. This is the strongest possible proof that the tool works. It's not a demo. It's not theoretical. It ate its own dogfood and shipped 33,500 lines of tested code doing it. **Lead with this story.**

2. **YAML-first is real** — LangGraph requires Python. CrewAI requires Python. AutoGen requires Python. Dify has a visual builder but no CLI/CI story. Orchemist is the only tool where a non-coder can define a multi-agent pipeline in YAML and run it from the command line.

3. **Scenario-based grading** — Nobody else has built-in acceptance testing for AI pipelines. The idea that a pipeline isn't "passing" until a scenario grades it is a unique and powerful concept. This is the moat.

4. **Test-to-code ratio (2:1)** — Signals real engineering, not a weekend project. This is rare in the AI tooling space. And it was achieved through the tool's own quality gates.

5. **Three execution modes** — Standalone (Anthropic API), OpenClaw (sub-agents), dry-run (testing). No vendor lock-in.

6. **"Docker Compose for AI pipelines"** — The positioning is correct. Lean into it harder.

### Competitive reality

| Competitor | Advantage over Orchemist | Orchemist's edge |
|---|---|---|
| LangGraph | Anthropic/Google backing, massive adoption | Requires Python code. No YAML-first. No built-in grading. |
| CrewAI | $18M funding, marketing momentum | Requires Python code. No scenario testing. |
| Dify | 50k+ GitHub stars, visual builder | No CLI/CI story. No acceptance testing. No version-controlled pipelines. |
| AutoGen | Microsoft backing | Complex. Requires Python. No template system. |

### Target audience (not the same as competitors)

Orchemist is not for ML engineers who want a Python framework. It's for:

- **DevOps engineers** who want AI pipelines that are version-controlled and CI-testable
- **Technical writers and content teams** who want to automate multi-step workflows without code
- **Small teams** who want to automate without hiring an ML engineer
- **Anyone who wants testable, auditable AI workflows** — the scenario grading system is the selling point

---

## 4. Launch Checklist (Ordered)

| # | Task | Effort | Blocks launch? |
|---|---|---|---|
| 1 | Run `orch validate` on all templates, fix failures | 1 hour | Yes |
| 2 | Run all templates in `--mode dry-run`, fix failures | 2 hours | Yes |
| 3 | Create `.github/workflows/ci.yml` | 2 hours | Yes |
| 4 | Publish to PyPI (`orchemist` 0.3.0) | 1 hour | Yes |
| 5 | Test `pip install orchemist` in clean env | 30 min | Yes |
| 6 | Write `docs/tutorial.md` (5-minute quickstart) | 3 hours | Yes |
| 7 | Clarify OpenClaw vs standalone in README | 1 hour | Yes |
| 8 | Set GitHub repo to public | 5 min | Yes |
| 9 | Write one blog post / announcement | 3 hours | No (but do it) |
| 10 | Run content-pipeline-v28 end-to-end with real API | 1 hour | No (but do it) |

**Total estimated effort: ~1.5 days of focused work.**

---

## 5. Post-Launch Priority

Once the repo is public and the package is on PyPI, the first feedback will tell you what to build next. Until then, the roadmap is speculation.

Likely first requests from real users:
1. "How do I add my own executor?" → Write an executor plugin guide
2. "Can I use GPT-4 instead of Claude?" → Add an OpenAI executor
3. "The web UI doesn't do X" → Prioritize based on what users actually try
4. "How do I test my templates?" → Expand the scenario authoring docs

**The 762-line roadmap is a vision document, not a launch requirement. Ship, listen, then decide what's next.**
