# Harness Redesign — investigation pack · 2026-05-24

This folder collects the investigation artifacts produced on **2026-05-24** in support of the **Orchemist Harness Redesign** epic. Nothing here is code — it is the briefing material that defines the epic scope, the engineering debt to clean up before the build starts, and the visual target for the new web surface.

## What is in this folder

| File | What it is | Read it when |
|---|---|---|
| [`VISION.md`](VISION.md) | Synthesis of `ROADMAP.md`, `docs/internal/level5-requirements.md`, and the 2026-05-21 pivot memory into one charter for the epic. Lists architectural pillars, anti-goals, and 5 open decisions that block kickoff. | First. This is the contract. |
| [`DUPLICATES.md`](DUPLICATES.md) | Audit of duplicate functions across `orchemist`, `orchemist-skills`, `orchemist-website`, and `orchemist-ide`. 7 groups identified; verdict-extraction trio is the highest-severity (issue #687). | When deciding what cleanup work to land before the harness redesign begins. |
| [`FRONTEND.md`](FRONTEND.md) | Inventory of the existing Next.js frontend in `frontend/`. Maps every page, component, and `lib/` module; flags the gaps the new harness must close. | When scoping which screens of the redesign are net-new vs. extensions of existing pages. |
| [`AUTONOMY.md`](AUTONOMY.md) | Branch-protection audit (all 4 repos currently unprotected) + the autonomy preconditions that block Level-5 promotion. Includes a proposed `gh api` script to enable protection. | When acknowledging the protection rules before the harness goes live. |
| [`screens/01-fleet-dashboard.svg`](screens/01-fleet-dashboard.svg) | Coherent design language for the new web harness — six cross-linked screens covering fleet, run cockpit, adversary loop, trust & gates, admin/activation, and skills-pack mode. | When commenting on visual direction or wireframe content. |
| [`screens/02-run-cockpit.svg`](screens/02-run-cockpit.svg) | ↑ |   |
| [`screens/03-adversary-loop.svg`](screens/03-adversary-loop.svg) | ↑ |   |
| [`screens/04-trust-gates.svg`](screens/04-trust-gates.svg) | ↑ |   |
| [`screens/05-admin-activation.svg`](screens/05-admin-activation.svg) | ↑ |   |
| [`screens/06-skills-pack-mode.svg`](screens/06-skills-pack-mode.svg) | ↑ |   |

## How the artifacts cross-reference

- **`VISION.md` §4 capability matrix** ↔ **`FRONTEND.md` Gaps for Level-5 harness UI** — the same gap shows up in both: what is shipped server-side but unrendered client-side.
- **`VISION.md` §7 open decisions** — those five answers shape what is in / out of the screens. The screens already encode my best guess; if any decision lands differently, the affected screen needs revision.
- **`DUPLICATES.md` Group 1 (verdict)** ↔ engine issue **#687**. Group 6 (`RunStatus` literal-union drift) is a new finding — file as a follow-up.
- **`AUTONOMY.md` §4 table** ↔ all four other documents — every row points at the artifact that closes the gap.

## How to consume this pack

1. Read `VISION.md` end-to-end (≈ 10 min). Stop at §7 and answer the five open decisions inline as PR comments or as a separate decision-record document.
2. Skim `DUPLICATES.md` headline summary + the three HIGH-severity groups (1, 6, the cross-language enum drift). Decide whether to land the consolidation PRs **before** the harness epic kicks off or in parallel. The recommendation is *before* — a clean target tree makes the harness epic's PR diffs more reviewable.
3. Open `FRONTEND.md` and `screens/01-fleet-dashboard.svg` side by side. The audit + the mockup together tell you what is reused vs. net-new in `frontend/`.
4. Acknowledge `AUTONOMY.md` §1 — running the included `gh api` snippet enables branch protection. This unblocks the L5 promotion criteria.

## Status of the artifacts

All five documents were produced in a single session on 2026-05-24 by Claude Code Opus 4.7 (1M context). The audit subagents (duplicate audit, frontend audit, vision synthesis) ran in their own fresh context windows per [[feedback_fresh_subagent_per_phase]]. The drafter's reasoning is **not** carried over into this folder — only the file outputs are.

These artifacts are **investigation grade**, not contract grade. The epic owner should treat them as a starting set; the formal behavioral contracts for each sub-issue will be authored when the issue is filed.
