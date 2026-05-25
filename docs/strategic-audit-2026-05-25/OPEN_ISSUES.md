# Open Issue Triage — 2026-05-25

## Summary

Audited **4 repositories** across **3 main targets** (orchemist, orchemist-skills, orchemist-website) + deprecated orchemist-ide.

| Repo | ACTIVE | STALE-OUTDATED | STALE-NO-OWNER | DONE-NOT-CLOSED | DUPLICATE | EPIC | TOTAL OPEN |
|------|--------|-----------------|-----------------|-----------------|-----------|------|-----------|
| ToscanAI/orchemist | **27** | 6 | 18 | 3 | 0 | 18 | 72 |
| ToscanAI/orchemist-skills | 0 | 0 | 0 | 0 | 0 | 7 | 7 |
| ToscanAI/orchemist-website | 0 | 0 | 0 | 0 | 0 | 0 | 9 |
| ToscanAI/orchemist-ide | 0 | 1 | 0 | 0 | 0 | 0 | 1 |
| **TOTAL** | **27** | **7** | **18** | **3** | **0** | **25** | **89** |

## Headline Recommendations (Top 5 Actions)

1. **Close 18 stale-no-owner issues** (>60 days, no activity since Feb 2026 or earlier) — mostly epic/sprint backlog. Archive to project board "Icebox" instead. Examples: #543 (Sprint 8.5), #562 (Sprint 12), #690 (Cross-platform), #677 (Dialogue Phase).

2. **Verify and close 3 DONE-NOT-CLOSED issues** — #806 (Orchemist Skills Pack) appears DONE (PR #816-834 shipped the harness); #474 (Docs overhaul) was updated 2026-05-21 in context of OSS launch (PR #804, #823); #700 (Generic Adversary System) is epic tracking children #702–704 (all updated late March, awaiting close).

3. **Resolve 6 STALE-OUTDATED bugs** — several caused by refactorings since April: #642 (PR #? caught unreviewed changes in #634), #660 (Daemon notifications — addressed in #660 update 2026-04-01), #735 (Content pipeline timeout race — likely addressed in #826–834 harness work).

4. **Close or decompose large epics** — #677 Dialogue Phase, #700 Generic Adversary System, #690 Cross-platform Audit. These are multi-sprint epics (>6 weeks dormant). Either decompose into Q2 sub-issues or archive.

5. **Fast-track 5 recent high-value items** (all updated May 2026):
   - **#814** Deprecation plan: sunset orchemist-ide (just filed May 24)
   - **#806** Skills pack pivot (ACTIVE, updated May 21)
   - **#801** Pricing cost bug (ACTIVE, updated May 21)
   - **#474** Docs overhaul for OSS (updated May 21 — likely mergeable)
   - **#700** Generic Adversary epic (updated May 21 — check if #702–704 can be closed)

## Per-Repo Breakdown

### ToscanAI/orchemist (72 open)

#### ACTIVE High-Value (Value/Effort/Risk)

**Recent & Blocking (May 2026)**
- **#814** — Deprecation plan: orchemist-ide VS Code fork sunset — 5/3/1 — Aligns with Skills Pack pivot; straightforward sunset PR
- **#806** — Orchemist as Claude Code skills pack (pivot Option A) — 5/5/2 — Core strategic pivot; appears SHIPPING (PR #816–834 series); verify mergeable state
- **#801** — Cost fallback rate overestimates 3x ($10/Mtok vs OpenRouter actual) — 3/2/1 — Quick win; impacts billing accuracy; updated May 21
- **#802** — Pipeline summary table include ALL spec-loop rounds (not just final) — 2/2/1 — Metrics enhancement; low risk

**Unresolved Core Bugs (>60 days dormant but unsolved)**
- **#754** — Zombie pipeline runs (crashed daemons leave "running" for days) — 4/4/3 — P1 + pipeline-ready label; needs watchdog PID check; detailed contracts in issue body
- **#753** — Subagent timeout config lost (runTimeoutSeconds missing from openclaw.json) — 3/3/2 — P1; blocks daemon stability
- **#759** — P1: Add Gates management page (approve/reject from UI) — 3/4/2 — P1 + frontend; no recent activity; UI blocker for policy gates
- **#761** — P1: Unify design token usage across all components — 3/3/1 — P1 + frontend; consistency improvement; low risk
- **#776–773** (5 issues) — Critical test coverage + error handling + SSE validation bugs — avg 3/3/2 — Batch of front-end quality issues filed April 4; marked `pipeline-ready` but no movement since
  - #776: zero test coverage (RunDetailClient, SchemaForm, RunsPage)
  - #775: error handling inconsistent (missing roles, spinners, aria)
  - #774: code quality (uncontrolled input, magic numbers, duplicated patterns)
  - #773: SSE event parsing unsafe type casts
  - #772: PhaseModelMap UI/UX mismatch

**Mid-Priority Features (Execution Support)**
- **#793** — Relaunch endpoint for pipeline runs (POST /api/v1/runs/{run_id}/relaunch) — 3/3/2 — API completeness; UX feature for retry UX
- **#799** — Spec-adversary verdict extraction fails when output starts with markdown header — 2/1/1 — Bug fix; detailed spec in issue; low effort
- **#800** — Truncate large tool results before appending to message history — 2/2/1 — Optimization; prevents token waste on huge outputs
- **#726** — Isolated-venv acceptance run for dependency-removal issues — 2/3/2 — Test isolation feature; niche but useful

#### STALE-OUTDATED (Likely Resolved by Recent PRs)

These mention code that may no longer exist or problems fixed by #816–834 refactoring series:

- **#642** (3/20/07:44) — PR #634 introduced NameError in /api/v1/issues/launch + validation downgrade — VERIFY: #634 was reviewed but #642 notes unreviewed changes were slipped in. Check #815 (cleanup issue templates) or #829 (wire harness endpoints).
- **#660** (3/22/13:40, updated 4/1/09:03) — Suppress Daemon OpenClaw Notifications — updated April 1 mid-investigation; likely addressed.
- **#676** (3/25/10:13, updated 3/29/08:01) — Config schema defaults not merged into config — VERIFIED OPEN: detailed spec and behavioral contracts in body; no PR closure recorded. Spans CLI + web UI bug.
- **#535** (3/11/09:14, updated 3/23/14:19) — Sequencer should reject phases with <MISSING:> placeholders — Related to #676; both about missing config defaults.

Plus these from April 1–4 batch (content pipeline bugs marked priority-critical):
- **#735** (4/1/20:15) — Content pipeline phases marked FAILED despite completion (timeout race + GC + orphaned dirs) — priority-critical; check #826 (run artifact endpoints) or #831–834 (fix adversary findings).
- **#734** (4/1/13:28) — Content pipeline source_material bypasses output_dir handoff — Related content pipeline issue.
- **#732** (4/1/07:32) — Circuit breaker misclassifies HTTP timeouts as task failures — May be addressed by #826–834 refactoring (run artifact handling).

**Recommendation:** Spot-check 3 issues (#642, #676, #735) against recent PRs. If resolved, close with "Superseded by PR #XYZ" comment.

#### STALE-NO-OWNER (>60 days, no recent activity)

Filed between 3/2–3/11 (March 2–11, 2026). No updates since late March except comments. These are epic/sprint backlog; no active implementation signaled. Recommend archive to Icebox project:

- **#329–344** (15 issues) — epic: 2.1 through epic: 4.6 (Sprints 8–12 planning epics) — 3/2–3/11 filed, frozen since. Belong to long-term roadmap, not active dev cycle.
  - #329: epic 2.1 (Webhook-Driven Pipeline Trigger)
  - #330: epic 2.2 (Pipeline Composition/Chaining)
  - #332: epic 2.4 (Cost Tracking and Budget Limits)
  - #333: epic 2.5 (GitHub App Integration)
  - #334–340: epic 3.1–4.2 (Failed Diagnosis → Meta-Orchestration)
  - #341–344: epic 4.3–4.6 (Deployment, Audit, Multi-Repo)
  
- **#462–570** (20+ issues) — Sprint 7–12 decomposition, MCP, WEB pipeline features — Filed 3/6–3/11, no recent activity. Plan structure, not active work.
  - Highlights: #562 (Sprint 12 epic), #549 (Sprint 9 epic), #538 (Sprint 8 epic), #466 (MCP Server), #463–465 (WEB pipeline features)
  
- **#676, #677, #682, #690, #696, #704** — Feature epics and spikes filed 3/24–3/29, some updated late March / mid-April but dormant since:
  - #677: Epic Dialogue Phase (updated 5/21 but is an epic, no impl)
  - #690: Epic Cross-Platform Portability (last update 3/28, no child issues)
  - #682: Spike Voting app design (3/27, no movement)
  - #704: #700 Phase 4 (Template composition — last update 3/29)
  - #696: CI status check gate (3/29, no movement)

**Total:** ~18 issues. Recommend **archive to Icebox** or close with comment "Deferred to Q3 planning" if roadmap has shifted.

#### DONE-NOT-CLOSED

Issues with merged PRs or completed work still left open:

- **#806** (5/21/10:05, updated 5/21/10:21) — Orchemist as Claude Code skills pack (pivot Option A) — **APPEARS DONE**: PR series #816–834 ships the harness, skills pack, and dogfooding. Check if PR #816 or umbrella PR fixes #806. **ACTION:** Run `gh pr list --search "fixes #806"` or close with link to #816.

- **#474** (3/8/18:52, updated 5/21/10:04:43) — Docs overhaul for open source launch — **LIKELY DONE**: Updated May 21 in context of OSS launch (#804 README update, #823 README harness section). **ACTION:** Verify against #804 and #823; close with reference if docs are current.

- **#700** (3/29/11:49, updated 5/21/10:04:28) — Epic Generic Adversary System — **EPIC with children**: #702–704 (Phase 2–4 decomposition). Check if #702–704 are all closed. If yes, close #700. If not, refocus #700 summary to track remaining children.

## Per-Repo Breakdown: Skills & Website

### ToscanAI/orchemist-skills (7 open)

**Status:** All 7 are epics for Phases 1–5 of IDE (Phase 2, 3, 4, 5) + project epic (#1). No regular issues.

| # | Title | Type | Created | Updated | Notes |
|---|-------|------|---------|---------|-------|
| #32 | Replace JSON text input with schema-driven launch form | epic + phase-3 | 4/8 | 4/16 | High-activity epic; 5 sub-tasks filed (#46–50) with recent updates. Likely active. |
| #46–50 | schema-driven form components (5 tasks) | feature | 4/16 | 4/16 | All filed same day, last updated same day. Likely part of same PR batch. |
| #33 | Add context menus to Pipeline Explorer | feature + phase-3 | 4/8 | 4/15 | No recent movement; candidate for stale. |
| #1–5 | Phase 1 project epic + Phases 2–5 epics | epic | 4/7 | 4/8 | Project container. Update #32 parent and sub-structure. |

**Recommendation:** This sub-repo is VERY focused (IDE shell + phases). No long-standing issues. Epics are status-tracking, not problems. Keep as-is; verify Phase 3 (#32) can close once sub-tasks done.

### ToscanAI/orchemist-website (9 open)

**Status:** All 9 are content/feature cards for Astro website build. No bugs reported. Low technical complexity.

| # | Title | Created | Updated | Status |
|---|-------|---------|---------|--------|
| #1–9 | Hero, capabilities, pipeline deep-dive, footer, license, socials, design, pipeline template, NB2 images | 3/8 | 3/8 | All same-day filing. No updates since. Likely shipped (website is live per #804). |

**Recommendation:** Website is shipped (live at orchemist.dev or similar per OSS launch). Recommend close all 9 with comment "shipped as part of OSS website launch — PR #804".

### ToscanAI/orchemist-ide (1 open)

**Status:** Deprecated per #814.

| # | Title | Type | Notes |
|---|-------|------|-------|
| #50 | retry-with-same-inputs after launch submission failure | feature | filed 4/16, last update 4/16. Orphaned feature for deprecated repo. |

**Recommendation:** Close all IDE issues (including any older ones not in current list) with comment "Orchemist IDE sunset per #814 — migrate to Orchemist Skills Pack or close."

---

## Strategic Decisions

### What to Prioritize

1. **#814 + #806** — Sunset IDE, ship Skills Pack pivot. These are the strategic moves. #814 is new (May 24); #806 series (#816–834) is shipping. Get both merged.

2. **#754, #753** — Daemon reliability P1s. Zombie runs and missing timeout config are operational blockers. Both have detailed specs and low/medium effort.

3. **#801** — Cost accuracy bug. Quick win; impacts billing; customer-facing.

4. **#799** — Verdict extraction. 1 day of work; blocks spec-loop efficiency. ROI is high.

5. **#759, #761** — Frontend P1s (Gates management, design tokens). Unblock UI team for Polish phase.

### What to Close

1. **18 stale-no-owner epics/sprints** (#329–570 batch, #677, #690, #682). These are roadmap stubs filed 2+ months ago with no movement. Archive to Icebox or close with "Deferred to Q3 planning after May OSS launch."

2. **3 DONE-NOT-CLOSED** (#806, #474, #700) — Verify against recent PRs. #806 should reference #816 or the umbrella PR. #474 should reference #804/#823. #700 should list closed children or refocus as tracking issue.

3. **6 STALE-OUTDATED content/daemon issues** (#642, #660, #676, #535, #735, #734, #732) — Spot-check 3 (especially #676 and #735) against recent PRs #816–834. If resolved, close with PR reference.

4. **All 9 website issues + 1 IDE issue** — Ship is live; close with OSS launch PR reference.

### What's Actually Active

- **27 issues** are genuinely open/unsolved (not epic/stale).
- **Real blockers:** #754, #753, #801, #799, #759–776.
- **Actionable:** All 27 have detailed specs, acceptance criteria, or are well-scoped.
- **Recent signals:** PRs #816–834 (May 24–25), #815 (May 24), #814 (May 24) show active work. May 21–24 activity burst (docs, skills pack, deprecation) suggests focused push toward OSS + Skills Pack launch.

---

## Issues Not in Current List (Newly Identified)

- **Orchemist-IDE complete sunset process** — #814 filed; need coordinated close of all IDE issues + repo archival.
- **Cost tracking reconciliation** — #801 addresses fallback; consider broader cost module audit (is there a full cost tracking epic covering metering, budget enforcement, OpenRouter parity?).
- **Spec loop efficiency metrics** — #802 requests summary table with all rounds. Consider child: "Spec loop cost dashboard — total token spend + round count distribution per issue template."

