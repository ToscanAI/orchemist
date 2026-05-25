# Orchemist Strategic Course of Action — 2026-05-25

Synthesises four independent audits run on 2026-05-25:
- [OPEN_ISSUES.md](OPEN_ISSUES.md) — 89 open issues triaged across 4 repos
- [RISK_ASSESSMENT.md](RISK_ASSESSMENT.md) — risk-document verification + 6 undocumented risks
- [DUPLICATES_REFRESHED.md](DUPLICATES_REFRESHED.md) — 3/7 original groups resolved; 2 new critical duplicates
- [VISION_GAP.md](VISION_GAP.md) — 7/8 architectural pillars realised; 1 critical gap

## TL;DR

- **27 actively unsolved issues** + **18 stale epics to archive** + **3 done-not-closed to verify** + **10 shipped issues to close** (website + IDE).
- **Phase 0 not in engine YAML** — UI shows it, engine never runs it. Marquee VISION gap. Fix: 1 day.
- **Verdict keyword duplication** — silent divergence risk between `verdict_parser.py` (lowercase) and `transitions.py` (uppercase). Fix: 1–2 hours.
- **5 undocumented risks** (PKI release SOP, admin.json audit log, SQLite WAL backpressure, feature_flags runtime gap, SSE limits). File now; defer fixes by severity.
- **Phase metadata duplicated** across YAML + 2 frontend files. Fix: 2–3 days (REST endpoint).

Branch protection on `orchemist` (ruleset 16835594) and `orchemist-skills` is active. `orchemist-website` still needs ruleset.

---

## Course of Action — Sequenced Plan

### Week 1 (immediate, 2026-05-26 → 2026-05-31)

**P0 — Correctness gaps that block VISION parity**
1. **Port Phase 0 (`existing_symbols_inventory`) to `templates/coding-pipeline-standard.yaml`.** Lift the YAML block from `orchemist-skills/pipelines/coding-pipeline-standard.yaml v4.2` (the canonical v4.2 definition) into engine YAML at position 0, before `spec`. Update `templates/coding-pipeline-standard.yaml` version → v3.0.0. Update CHANGELOG. **Owner: engine.** **Cost: 1 day.** Tracking: NEW ISSUE A.
2. **Consolidate `_VERDICT_KEYWORDS`.** Delete the uppercase tuple in `transitions.py:95`; re-export the lowercase set from `verdict_parser.py`. Audit `sequencer.py:3363` for case-sensitivity (the `.upper()` callsite needs to stay or the comparison needs lowercase). **Cost: 1–2 hours.** Tracking: NEW ISSUE B.

**P0 — Backlog hygiene (no code, just GH ops)**
3. **Close shipped-but-open issues.** Verify and close: #806 (skills pack — superseded by PRs #816–834), #474 (docs overhaul — superseded by PR #804/#823), #700 (epic — confirm children #702–704 closed first). All 9 `orchemist-website` issues (shipped per #804). The 1 `orchemist-ide` issue (sunset per #814). **Cost: 1 hour of `gh` commands.**
4. **Archive 18 stale epics to Icebox.** Close with comment "Deferred to Q3 planning after May OSS launch": batch #329–344, #462–570, #677, #682, #690, #696, #704. **Cost: 30 min.**

### Week 2 (2026-06-01 → 2026-06-07)

**P1 — File undocumented risks (no fixes, just visibility)**
5. File NEW ISSUES C–G (see "New Issues to File" below).
6. **Republish AUTONOMY.md** with current branch-protection state (`orchemist` + `orchemist-skills` ✅, `orchemist-website` ⏳, `orchemist-ide` deprecated). **Cost: 15 min doc edit.**

**P1 — Operational bug fixes that have detailed specs**
7. **#754 Zombie pipeline runs** (watchdog PID check). **Cost: 2 days.**
8. **#753 Subagent timeout config lost** (`runTimeoutSeconds` missing from `openclaw.json`). **Cost: 1 day.**
9. **#801 Cost fallback overestimates 3×** (quick win, customer-facing). **Cost: 1 day.**
10. **#799 Spec-adversary verdict extraction** on markdown-header outputs. **Cost: 1 day.**

### Week 3+ (2026-06-08 →)

**P2 — Frontend quality batch (#772–776, #759, #761)** — 5 frontend P1s clustered around test coverage, error handling, SSE validation, Gates UI, design tokens. Bundle into one milestone "Frontend hardening Q2"; assign single owner; ship in 1.5–2 sprints.

**P2 — Phase metadata REST endpoint (NEW ISSUE H).** Adds `GET /api/v1/phases`. Removes hardcoded phase arrays from `RunDetailClient.tsx` and `skills/page.tsx`. **Cost: 2–3 days.**

**P2 — Feature_flag runtime wiring (NEW ISSUE F).** Either wire flags into sequencer/daemon, or remove from admin UI. Right now the UI is a false control. **Cost: 3–5 days OR remove from UI in 1 hour.**

---

## New Issues Filed (2026-05-25)

| # | Title | Severity | Effort | Source |
|---|---|---|---|---|
| [#835](https://github.com/ToscanAI/orchemist/issues/835) | Port Phase 0 (existing_symbols_inventory) to engine YAML | CRITICAL | 1d | VISION_GAP |
| [#836](https://github.com/ToscanAI/orchemist/issues/836) | Consolidate _VERDICT_KEYWORDS into single source-of-truth | LOW (latent HIGH) | 1–2h | DUPLICATES |
| [#837](https://github.com/ToscanAI/orchemist/issues/837) | Document & enforce PyPI release SOP (signed tags + 2-person review) | HIGH | 1d (doc) | RISK |
| [#838](https://github.com/ToscanAI/orchemist/issues/838) | Document `~/.orchestration-engine/` config surface + add admin.json audit log | MEDIUM | 2d | RISK |
| [#839](https://github.com/ToscanAI/orchemist/issues/839) | Add SQLite WAL concurrent-daemon backpressure | MEDIUM | 3d | RISK |
| [#840](https://github.com/ToscanAI/orchemist/issues/840) | Wire admin feature_flags into sequencer runtime (or remove from UI) | MEDIUM | 3–5d | RISK |
| [#841](https://github.com/ToscanAI/orchemist/issues/841) | Add SSE connection limits + per-IP rate limiting | LOW | 1d | RISK |
| [#842](https://github.com/ToscanAI/orchemist/issues/842) | Expose `GET /api/v1/phases` and remove hardcoded phase arrays from frontend | MEDIUM | 2–3d | DUPLICATES |

Risk #6 from RISK_ASSESSMENT (artifact endpoint path traversal) was **verified safe** in `api.py:2095–2102` (filename split check + `target.relative_to(out_dir)`). No issue needed.

---

## Issues to Close

**DONE-NOT-CLOSED (verify and close):**
- `orchemist#806` — superseded by PRs #816–834 (Skills Pack Mode shipped)
- `orchemist#474` — superseded by PRs #804, #823 (OSS docs landed)
- `orchemist#700` — verify children #702–704 closed; refocus or close

**SHIPPED (close with PR reference):**
- All 9 `orchemist-website` issues — shipped via PR #804
- `orchemist-ide#50` (and any other open IDE issues) — sunset per #814

**ARCHIVE TO ICEBOX (close with "Deferred to Q3"):**
- Stale-no-owner epics: #329–344 (Sprints 8–12 planning), #462–570 (Sprint 7–12 decomposition + MCP/WEB pipeline features), #677, #682, #690, #696, #704

**STALE-OUTDATED (spot-check then close if resolved):**
- #642, #676, #735 — verify against recent PRs #816–834; close with "Superseded by PR #X" if applicable

---

## What's NOT Recommended

- **Generic adversary refactor (#700/#702–704)** — anti-goal per VISION. Keep dedicated adversaries.
- **NL pipeline builder in UI** — anti-goal per VISION.
- **Multi-repo orchestration in MVP** — open decision #4; defer until after Q2.
- **Reviving orchemist-ide** — sunset per #814. Frontend in `orchemist/frontend/` is the replacement.

---

## Open Strategic Decisions

These require a human call before next milestone:

1. **orchemist-ide deprecation pace** (#814) — immediate archive vs. sunset-tracked README redirect?
2. **Skills ↔ engine sync direction** — Is `orchemist-skills/pipelines/` canonical (engine pulls from skills) or is `orchemist/templates/` canonical (skills publishes from engine)? Affects how Phase 0 port lands.
3. **External validator subprocess** (VISION decision #5) — `validator.py` is partial; commit or revert.
4. **Multi-repo MVP scope** (VISION decision #4) — single-repo only, or add repo switcher?

---

## Metrics

| Metric | Value |
|---|---|
| Open issues across 4 repos | 89 |
| Actively unsolved (non-epic, non-stale) | 27 |
| Issues recommended to close this sprint | 31 (3 done + 10 shipped + 18 stale) |
| New issues to file | 8 |
| Critical gaps in VISION | 1 (Phase 0 missing) |
| Critical duplicates (latent divergence risk) | 2 (verdict keywords, phase metadata) |
| Risks documented vs. undocumented | 3 docs covering 6 known risks; 5 risks undocumented |
| Cleanup PRs landed in last 7d | 22 |
| Lines of duplicate code consolidated (PRs #818/#820/#824) | 571 |

---

*Strategic audit closed 2026-05-25. Re-run quarterly or after any major architectural shift.*
