# Duplicate-Function Audit Refresh — 2026-05-25

## Summary

**Original groups status:** 3 of 7 fully resolved by cleanup PRs (#818, #820, #821, #824); 4 remain open or partially resolved. **New duplicates found:** 2 critical ones (phase list duplication, verdict keyword duplication).

---

## Status of Original 7 Groups

| Group | Original Severity | Resolved by | Status | Lines Saved |
|-------|-------------------|------------|--------|------------|
| 1. verdict_parser drift | HIGH | #820 | ✅ Resolved (1 extract_verdict) | ~491 |
| 2. file-hash trio | MEDIUM | #818 | ✅ Resolved (single compute_hash) | ~30 |
| 3. slugify drift | MEDIUM | #824 | ✅ Resolved (text_utils.py) | ~50 |
| 4. retry/backoff scatter | LOW | — | ⚠️ OPEN | — |
| 5. git operations split | LOW | — | ⚠️ OPEN | — |
| 6. RunStatus/TaskState enum drift | HIGH | ❌ NOT RESOLVED | ⏳ PENDING #821 partial | — |
| 7. verdict format prose | MEDIUM | — | ⚠️ OPEN | — |

**Verification:**
- Group 1: `grep -rn "def extract_verdict"` returns 1 result (verdict_parser.py:48) ✅
- Group 2: `grep -rn "def _compute_hash"` returns 0 results; only file_guard.compute_hash exists ✅
- Group 3: `grep -rn "def slugify"` returns 2 results in text_utils.py (slugify + slugify_branch) — unified API ✅
- Group 6: frontend/lib/types.ts still declares separate RunStatus type (lines 98–106); no auto-generated sync ⚠️

---

## NEW Duplicates Found

### NEW Group A: Pipeline Phase Metadata — CRITICAL (Frontend ↔ YAML)

**Locations:**
- `templates/coding-pipeline-standard.yaml:66–`  (11 phase entries: spec, behavioral, spec_adversary, postmortem_spec, acceptance_test, implement, acceptance_run, review, fix, postmortem_review, test)
- `frontend/app/runs/[id]/RunDetailClient.tsx:45–56` (hardcoded PHASES array, 10 entries)
- `frontend/app/skills/page.tsx:26–37` (hardcoded PHASE_CARDS array, 10 entries, different structure/naming)

**Why it's a duplicate:**
- Phase order, IDs, and labels are defined in THREE independent places
- Frontend has no sync mechanism with YAML source-of-truth
- `RunDetailClient.tsx` omits postmortem phases (postmortem_spec, postmortem_review); `skills/page.tsx` uses abbreviated names ("existing_symbols" instead of "existing_symbols_inventory")
- New phases added to YAML after 2026-05-24 require manual updates in both TSX files
- Divergence risk: if YAML adds a phase and TSX is forgotten, the UI becomes out of sync with orchestrator

**Severity:** **CRITICAL** — User-facing mismatch between UI pipeline visualization and actual phase execution.

**Consolidation proposal:**
1. Export phases list from backend as REST endpoint: `GET /api/v1/phases`
2. Frontend fetches at boot time and derives UI state from canonical backend list
3. Return shape: `{ id, label, subtitles, tier }[]`
4. Remove hardcoded PHASES and PHASE_CARDS arrays; fetch and memoize

**Risk/cost:** MEDIUM (2–3 days). HTTP roundtrip required; minimal caching overhead.

---

### NEW Group B: Verdict Keywords — CRITICAL (Case Mismatch + Duplication)

**Locations:**
- `src/orchestration_engine/verdict_parser.py:23` — `_VERDICT_KEYWORDS = {"approve", "request_changes", "abort"}` (lowercase)
- `src/orchestration_engine/transitions.py:95` — `_VERDICT_KEYWORDS = ("APPROVE", "REQUEST_CHANGES", "ABORT")` (UPPERCASE)

**Why it's a duplicate:**
- Same semantic set; different case convention
- verdict_parser returns lowercase (per line 19 docstring: `extract_verdict()` returns lowercase)
- transitions.py uses UPPERCASE (legacy); imports from verdict_parser (line 97) but doesn't use the imported one
- sequencer.py (line 36) imports _VERDICT_KEYWORDS from transitions (uppercase) but also calls extract_verdict from verdict_parser (lowercase)
- If a new verdict keyword is added, TWO definitions must be updated

**Severity:** **CRITICAL** — Latent divergence risk; uppercase/lowercase mismatch could silently cause verdict extraction to fail if transitions.py logic is refactored.

**Consolidation proposal:**
1. Keep single source-of-truth in verdict_parser.py (line 23), lowercase
2. In transitions.py, remove local definition (line 95)
3. Replace with: `from .verdict_parser import _VERDICT_KEYWORDS as VERDICT_KEYWORDS` (export as UPPERCASE if needed for clarity)
4. Audit all callsites (sequencer, transitions, any tests) for case-sensitivity bugs
5. Normalize all uses to lowercase (matches extract_verdict output)

**Risk/cost:** LOW (1–2 hours). Purely mechanical consolidation; test coverage exists.

---

### NEW Group C: Timestamp Normalization — MEDIUM (API layer isolation)

**Location:**
- `src/orchestration_engine/web/api.py:2368–2393` (internal helpers _NAIVE_ISO_RE, _normalize_ts, _normalize_row)

**Pattern:** Regex match + conditional transform (ISO timestamp normalization for DB row serialization).

**Why it's worth flagging (not a hard duplicate):**
- Self-contained in api.py; used 3 times (lines 2452, 2501, 2539) in GET /runs endpoints
- Not duplicated elsewhere YET
- **Future risk:** if other endpoints need similar normalization, this pattern will be copied

**Consolidation proposal:** Keep as-is (isolated to api.py); document as internal API layer utility. If 2+ additional endpoints need it, extract to db_utils.py.

**Risk/cost:** LOW (zero now; defer if pattern repeats).

---

## Recommended Next Actions (Priority Order)

1. **NEW Group B (Verdict Keywords)** — 1–2 hours
   - Consolidate _VERDICT_KEYWORDS into single source (verdict_parser.py)
   - Audit callsites for case-sensitivity

2. **NEW Group A (Phase Metadata)** — 2–3 days
   - Add `GET /api/v1/phases` REST endpoint
   - Frontend hydrates from endpoint instead of hardcoded arrays
   - Unblocks UI correctness for future phase additions

3. **Group 6 (Enum Sync)** — 5–7 days (medium-long term)
   - Add RunStatus enum to Python schemas.py
   - Auto-generate frontend types from Python via datamodel-code-generator

4. **Group 4 (Retry Config)** — 2–3 hours
   - Consolidate retry logic into RetryPolicy class

5. **Group 7 (Verdict Format Prose)** — 1–2 hours
   - Add GROUND TRUTH pointer in skills markdown

---

## Metrics

**Cleanup PRs impact (2026-05-24 → 2026-05-25):**
- Lines consolidated: 571 (Groups 1–3)
- Open duplicates: 4 groups + 2 new ones identified

**NEW duplicates:** 2 critical (phase list, verdict keywords), 1 medium (timestamp normalization).

