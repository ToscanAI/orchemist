# VISION vs IMPLEMENTATION GAP — 2026-05-25

## TL;DR — Status per Architectural Pillar

| Pillar | Status | Notes |
|---|---|---|
| 1. Ground-truth anchoring | ✅ Realised | 7 anchors in spec phase |
| 2. Adversarial quality gates | ✅ Realised | spec_adversary + review both Opus |
| 3. ATDD sealing | ⚠️ Partial | `file_guard.py` exists, hash guard wired, but `protect_on_approve` only on one template (coding-pipeline-skip-spec.yaml) |
| 4. Phase 0 inventory | ❌ Missing in engine | Present in `orchemist-skills v4.2` pipeline YAML; **absent from `orchemist/templates/coding-pipeline-standard.yaml`** |
| 5. YAML-first | ✅ Realised | `templates/` is source of truth |
| 6. Fresh subagent per phase | ✅ Realised | Skill .md files all use Agent tool |
| 7. One-file-per-phase | ✅ Realised | `output_dir` convention |
| 8. Max-effort adversary+review | ✅ Realised | `model: "opus"` in both phases |

## Key Findings

**CRITICAL GAP: Phase 0 missing from engine YAML.** `orchemist-skills/pipelines/coding-pipeline-standard.yaml v4.2` has Phase 0 (`existing_symbols_inventory`) as the first phase. `orchemist/templates/coding-pipeline-standard.yaml v2.0.0` lacks it entirely (only 11 phases). Frontend already renders Phase 0 ("0 · existing_symbols_inventory · v4.2") at `frontend/app/runs/[id]/RunDetailClient.tsx:46`. Net effect: UI promises a phase the engine never runs, so phase 0 cards will always be empty for engine-launched runs.

**Track B (Dialogue Phase) shipped.** PR #808 merged. `dialogue_phase.py`, `gemini_cli_executor.py`, `spec-review-dialogue.yaml` all present; 18 tests pass. Frontend `/adversary/page.tsx` reads dialogue artifacts via `?run=<id>`.

**Harness Screens: 6/6 operational.**
- Fleet Dashboard: KPI cards + runs table + autonomy ramp + regression queue + stale rail ✅
- Run Cockpit: phase rail + artifacts + cost/confidence ✅
- Adversary Visualizer: real dialogue rendering ✅
- Trust & Gates: approval queue + trust profiles + audit trail ✅
- Admin Console: autonomy ramp, modes, feature flags, kill switches ✅
- Skills Pack Mode: static pack metadata with v4.2 phase definitions ✅

**Anti-goals.**
- `orchemist-ide` still exists (sunset decision unanswered) — see open decision #1
- No generic adversary (#700) refactor — anti-goal upheld ✅
- No NL pipeline builder in UI ✅
- No force-push affordance ✅

## Open Decisions (§7 of VISION) — Status

1. ❓ orchemist-ide deprecation pace — IDE folder present, no README redirect yet
2. ⚠️ Embed skills pack mode — partial (deep link reference, unclear full scope)
3. ✅ Track B merge timing — PR #808 merged, visualizer wired
4. ❓ Multi-repo orchestration in MVP — no evidence; single-repo only
5. ❓ External validator subprocess — unclear (validator.py partial)

## Recommended Course of Action (Top 5)

1. **URGENT: Sync Phase 0 to engine YAML.** Port `existing_symbols_inventory` phase from `orchemist-skills v4.2` into `orchemist/templates/coding-pipeline-standard.yaml` before the `spec` phase. Blocks v4.2 capability parity. File: GH issue.

2. **Freeze `orchemist-ide`.** Add README.md redirect + deprecation notice → `/frontend`. Decide: immediate or sunset-tracked. (Issue #814 already exists — fast-track.)

3. **Document skills ↔ engine sync strategy.** Clarify: which is canonical (`orchemist-skills` or `orchemist/templates`)? File: ADR document.

4. **Resolve decision #4 (multi-repo MVP scope).** Audit frontend for repo switcher; document go/no-go.

5. **Verify Phase 0 config schema.** Ensure `ui_primitive_paths`, `lib_paths` etc. are honoured in `config_schema` when Phase 0 lands in engine YAML.

---

*Audit completed 2026-05-25. Verified across `templates/`, `frontend/app/`, `src/orchestration_engine/`, `dialogue_phase.py`, and `orchemist-skills/pipelines/`.*
