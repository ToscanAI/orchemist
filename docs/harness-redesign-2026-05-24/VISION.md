# Orchemist Vision Synthesis for Harness Redesign — 2026-05-24

> **Purpose.** This document is the charter for the harness redesign epic. It synthesises the North Star (ROADMAP.md), the Level-5 requirements doc (docs/internal/level5-requirements.md), the 2026-05-21 pivot (memory: `project_orchemist_pivot_2026_05_21`), and the active dialogue-phase prototype (PR #808 on `feat/677-dialogue-phase`).
>
> The "Architectural pillars" and "Anti-goals" sections are non-negotiable constraints on the new UI. The "Open decisions" section is the input vector — René decides those, and the UI scope is locked.

---

## 1. The North Star

Level 5 (Dark Factory) — `ROADMAP.md` §"North Star: A Day in the Life of Level 5".

> *Tuesday morning. You open GitHub and see 14 issues closed overnight. (…) 09:47 PM — a user opens issue #892: "CSV export truncates rows > 10,000." Orchemist's issue watcher picks it up, classifies it as a bug (confidence: 0.91), spawns a coding pipeline. 10:14 PM — Implementation complete. The review agent flags a missing edge case. A fix pipeline runs. (…) 10:16 PM — Confidence score is 0.93, above this repo's auto-merge threshold of 0.90. CI runs. Green. PR merges. (…) You review 3 items that need human judgment. Everything else handled itself. The factory ran in the dark.*

The harness UI must make this scenario operable by **one human reviewing one queue of exceptions**. Everything else is automation, dashboards, and audit trail.

---

## 2. Architectural pillars (load-bearing — must survive the redesign)

| # | Pillar | Source | Where it lives in code |
|---|---|---|---|
| 1 | **Ground-truth anchoring** — every phase prompt is anchored to the original issue body; agents cannot invent features | `README.md` §"Why Orchemist", PRs #789/#790/#791 | All phase prompts in `src/orchestration_engine/templates/*.yaml`; enforced via `{config[issue_body]}` placeholder discipline |
| 2 | **Adversarial quality gates** — separate agent reviews drafter at spec, behavioral, and code-review boundaries | `ROADMAP.md` §Sprint 7-10; `docs/internal/dialogue-harness-design.md` | `spec_adversary.py`, `acceptance_test_adversary.py`, `review_parser.py`, `verdict_parser.py` |
| 3 | **Acceptance-test-driven development** — tests written *before* implementation by a *separate* agent; implementer cannot modify them | `ROADMAP.md` §Sprint 7.1-7.3 | `file_guard.py` (SHA-256 sealing); `test_store.py` (immutable test store); `validator.py` (engine-executed runs) |
| 4 | **Phase 0 existing-symbols inventory** — sticky pre-pipeline grep of existing code; downstream phases read this artifact before authoring | `orchemist-skills/CHANGELOG.md` v4.0/v4.2; `coding-pipeline-standard.yaml:140-261` | `pipelines/coding-pipeline-standard.yaml :: existing_symbols_inventory`; consumed by SPEC, BEHAVIORAL, SPEC_ADVERSARY, IMPLEMENT, REVIEW |
| 5 | **YAML-driven pipeline templates** — version-controlled, diff-friendly, the only source of truth for phase sequencing | `ARCHITECTURE.md` §Template Engine; `pyproject.toml` ships `templates/` | `src/orchestration_engine/templates.py`; `templates/` directory |
| 6 | **Fresh subagent per phase** — each phase runs as an isolated agent with its own context window | Memory `feedback_fresh_subagent_per_phase`; encoded structurally in `orchemist-skills/skills/orchemist-*.md` Step 1 (delegate) | Engine: `executor.py` + `runner.py` dispatch; Skills: `Agent` tool invocation per phase |
| 7 | **One-file-per-phase artifact handoff** — each phase writes one immutable file under `{output_dir}/`; subsequent phases read by path | `coding-pipeline-standard.yaml` v2.0.0 description ("eliminating the bifurcation bug") | `output_dir` convention in every phase prompt |
| 8 | **Max-effort model for adversary + reviewer** — spec_adversary + review use Opus-tier reasoning | Memory `feedback_max_effort_adversary_reviewer` | `orchemist-skills/skills/orchemist-adversary.md` + `orchemist-review.md` set `model: opus` on the Agent call |

The harness UI must surface these pillars; it must not invite users to circumvent them.

---

## 3. The "trust engine" wedge (2026-05-21 pivot)

**What:** Cross-model adversarial review at phase boundaries. Different models — different families, ideally — review each other's work in multi-round loops until convergence (`APPROVED` verdict or `max_iterations` exhaustion). Track A (skills pack) is the first cut — adversary subagent in a different context window, same Claude family. Track B (dialogue phase, PR #808) is the full-stack version — drafter on Claude, reviewer on Gemini, Jaccard-drift convergence indicator, cost-tracked per turn.

**Why defensible** (2026 SDD market analysis from the pivot memory): Claude Code, Spec Kit (93k⭐), BMAD (46.7k⭐), CrewAI (44.5k⭐), Tessl — none ship cross-model adversary at the spec→implementation boundary. They're single-model multi-agent (CrewAI), single-model with role prompts (Spec Kit), or single-model with skills (Claude Code). Orchemist's wedge is the only one that mechanically prevents the same model rubber-stamping its own work.

**How it maps in product:**
- **Track A — Orchemist Skills Pack** (https://github.com/ToscanAI/orchemist-skills): pure markdown, runs inside Claude Code. Published 2026-05-21, currently at pipeline-YAML v4.2 (CHANGELOG entry 2026-05-24 — EXTEND verdict). Distribution surface for the IP.
- **Track B — Dialogue Phase (PR #808)**: engine-side `dialogue_phase.py` (794 LOC), `executors/gemini_cli_executor.py` (269 LOC), `templates/spec-review-dialogue.yaml`, 18 tests passing 2026-05-21. Reuses `verdict_parser.extract_verdict`. Jaccard drift 0.95 threshold over two consecutive pairs. Cost via `TaskResult.cost_usd`.

**Harness UI implication:** The adversary-loop visualizer is the marquee screen. Every other screen orbits it.

---

## 4. Level-5 capability matrix (current → target)

From `ROADMAP.md` §"Where We Are Today (Level 4)" + §"What's Missing for Full Autonomy".

| Capability | Status | Owner module | UI surface (proposed) |
|---|---|---|---|
| Multi-agent pipelines | Shipped | `PhaseSequencer`, `StateMachineSequencer` | Run cockpit |
| State-machine transitions | Shipped | `transitions.py` | Run cockpit (transition graph) |
| Review→fix loops | Shipped | Supervisor hooks, `APPROVE/REVISE/ABORT` | Adversary visualizer |
| Acceptance-test sealing | Shipped (Sprint 7) | `file_guard.py`, `test_store.py` | Run cockpit (seal badge) |
| Confidence-based routing | Shipped | `routing.py` | Trust + Gates page |
| Cost tracking & budgets | Shipped | `cost_tracker.py` | Header strip + Trust page |
| Webhook triggers | Shipped | `webhooks.py` | Admin / Activation |
| GitHub App integration | Shipped | `github_app.py` | Admin / Activation |
| Failure diagnosis | Shipped | `diagnosis.py` | Run cockpit (failure card) |
| Adaptive retry | Shipped | `adaptive_retry.py` | Run cockpit (retry ladder) |
| Regression detection | Shipped | `regression.py` | Fleet dashboard (regression queue) |
| Issue → pipeline automation | Shipped | `issue_automation.py` | Fleet dashboard (intake row) |
| Trust calibration engine | Shipped | `trust.py` | Trust + Gates page |
| OpenRouter tool-calling parity | Shipped (#794) | `openrouter_executor.py` | Admin / Activation (mode toggle) |
| Phase 0 inventory | Shipped (v4) | Pipeline YAML | Run cockpit (Phase 0 card with verdict table) |
| EXTEND verdict | Shipped (v4.2) | Pipeline YAML | Adversary visualizer (verdict chip) |
| **Fleet monitoring dashboard (3.4)** | **Missing** | — | Fleet dashboard (this is the gap) |
| **Stale detection (3.5)** | **Missing** | — | Fleet dashboard (stale rail) |
| **Meta-orchestration (4.2)** | **Missing** | — | Out of MVP |
| **Multi-repo orchestration (4.6)** | **Sprint 12 open** | `sprint_chain.py` partial | Fleet dashboard (repo switcher) |
| **External validator subprocess** | **Sprint 8 open** | `validator.py` partial | Run cockpit (validator badge) |
| **Audit trail / compliance export** | **#565 open** | — | Trust + Gates (export button) |
| **Dialogue phase (Track B)** | **Draft PR #808** | `dialogue_phase.py` | Adversary visualizer |

The harness redesign **closes** the missing rows. Everything else is rendering shipped engine functionality.

---

## 5. UI / Harness implications

### Screens (six, cross-linked via left rail)

1. **Fleet Dashboard (home)** — multi-repo strip, in-flight runs, gates needing review, regression queue, autonomy-level ramp toggle, cost-burn-rate widget.
2. **Run Cockpit** — phase-by-phase live view from Phase 0 (existing_symbols inventory) through test verification, with cost+time accumulators, model identity per phase, seal/hash badges, current confidence score trending.
3. **Adversary / Dialogue Visualizer** — drafter↔reviewer turn-by-turn with Jaccard drift sparkline, verdict timeline, model identity per side, cost per turn.
4. **Trust + Gates** — per-(repo, template, task_type) matrix of trust scores + auto-merge thresholds; manual approve/reject queue for items below threshold; audit-trail export.
5. **Admin / Activation Console** — feature toggles per environment, autonomy-level ramp (3 → 4 → 5), per-pipeline kill switch, branch-protection enforcement status, webhook trigger management, OpenRouter / Claude-subscription / OpenClaw mode selector.
6. **Skills Pack Mode** — local Claude Code skills pack view: install state, pipeline-YAML version (v4.2 today), last local run, link to remote engine instance for promotion.

### Activation toggles (Admin console scope)

- Auto-merge confidence thresholds — global default + per-repo override.
- Autonomy ramps — new repos start at "human-review-only", earn autonomy by trust calibration history.
- Trust-profile manual override — force-escalate or demote a (repo, template) confidence threshold.
- Adversary executor selector — Claude Opus subagent (Track A) or Gemini CLI (Track B dialogue).
- Phase-skip — pre-place `spec.md`+`behavioral.md`, use `coding-pipeline-skip-spec.yaml`.
- Phase 0 hard-gate flag — graceful-degrade (default) or HALT-on-missing-inventory (v4.2 `phase0_hard_gate: true`).

### Intentionally NOT in the UI

- **No force-push affordance** — `ROADMAP.md` §guardrails: "no force push, ever". Enforced in `git_integration.py`; UI must not invite it.
- **No destructive run deletion** — runs are immutable audit records; archive only.
- **No per-phase raw model picker** — model tier is set per template, not per run; ad-hoc overrides defeat reproducibility.
- **No free-text prompt editor** — prompts live in YAML templates, version-controlled; UI can scaffold templates, not generate prompts.
- **No "chat with your pipeline"** — natural-language workflow building is unreliable; YAML is the contract.

---

## 6. Anti-goals (what the harness must NOT become)

From the 2026-05-21 pivot memory + ROADMAP guardrails:

1. **Not a rebranded orchemist-ide fork.** The VS Code fork is 6 months behind Cline and is being **dropped**. New harness is the Next.js app already shipping in `orchemist/frontend/`, extended.
2. **Not the generic adversary system refactor (#700).** The dialogue wedge is opinionated — drafter + reviewer, multi-round, cross-model. Don't over-abstract into a pluggable adversary plugin architecture; that's a separate epic.
3. **Not a 30-bug cleanup project.** Harness redesign is orthogonal to the engine bug backlog. Triage separately.
4. **Not an npm/pip distribution channel for skills.** Track A (orchemist-skills) is pure markdown; distribution is `git clone + ./install.sh`. Whether to publish to a registry is René's call, not implied by the harness.
5. **Not a "natural language pipeline builder".** Templates are YAML. UI can scaffold them, not generate them.

---

## 7. Open decisions René needs to make

These shape harness MVP scope. They block kicking off the implementation epic.

1. **orchemist-ide deprecation pace.** Immediate freeze with a `README.md` redirect to the harness, or parallel-track sunset (no new features, security-only patches, deprecate at MVP launch)?
2. **Embed skills pack mode?** Can users run the coding pipeline in Claude Code from inside the harness (via "open in Claude Code" deep link), or is the harness engine-only and skills are a separate distribution?
3. **Track B (dialogue PR #808) merge timing.** Land on `main` before harness MVP starts (so the adversary visualizer shows real data), or iterate in parallel (visualizer ships with placeholder data, swaps to real once #808 merges)?
4. **Multi-repo orchestration in MVP.** Day-1 multi-repo fleet view, or single-repo only and multi-repo behind a flag?
5. **External validator (Sprint 8) shape.** Subprocess isolation in MVP (security + audit story tighter), or in-process for MVP (simpler) with subprocess as v2?

Each of these gets an explicit answer in the epic issue body before any implementation PR opens.

---

## Sources

- `ROADMAP.md` (the North Star scenario, capability matrix)
- `docs/ARCHITECTURE.md` (C4 diagrams)
- `docs/internal/level5-requirements.md` (Phase 2/3/4 functional + technical requirements)
- `docs/internal/dialogue-harness-design.md` (Track B architecture)
- `README.md` (`orchemist/`, `orchemist-skills/`)
- `orchemist-skills/CHANGELOG.md` (v4 / v4.1 / v4.2 entries — Phase 0 + EXTEND rationale)
- `pipelines/coding-pipeline-standard.yaml` (Phase 0 header comment is the densest single source on the inventory rationale)
- Memory: `project_orchemist_pivot_2026_05_21`, `project_orchemist_ide_status`, `project_orchemist_tool_parity`, `project_orchemist_pipeline_grounding`, `feedback_fresh_subagent_per_phase`, `feedback_max_effort_adversary_reviewer`.
