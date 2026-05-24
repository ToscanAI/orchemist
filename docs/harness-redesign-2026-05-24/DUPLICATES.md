# Orchemist Duplicate Function Audit — 2026-05-24

## Executive summary

Found 7 duplicate groups spanning Python intra-engine (4), Python within-engine (2), and TypeScript/cross-language (1). The dominant category is intra-engine verdict/output parsing logic with divergent implementations in `verdict_parser.py` and `review_parser.py`. Most critical consolidation candidate: verdict extraction family, where three independent implementations (`verdict_parser`, `review_parser.extract_verdict`, and prose rules in skills markdown) create shipping risk and maintainability debt. Phase 0 inventory system is critical—recommend enforcing it during pull request review.

---

## Group 1: Verdict Extraction — Three Implementations, Drift Risk (Issue #687)

**Locations:**
- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/verdict_parser.py` — `extract_verdict()` (lines 48–92)
  - Two-pass: structured `VERDICT: <keyword>` lines (Pass 1) + full-text regex fallback (Pass 2)
  - Supports `scan_order` parameter for reverse-scan (last match) vs. forward-scan (first match)
  - Returns lowercase `"approve"`, `"request_changes"`, `"abort"` or `None`

- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/review_parser.py` — `extract_verdict()` (lines 348–427)
  - Four-layer cascade: quick 5-line scan → smart full-text scan → tail-weighted scan → Haiku stub → None
  - Skips lines in fenced code blocks; checks for negative-prefix context
  - Returns uppercase `"APPROVE"` or `"REQUEST_CHANGES"` or `None`

- `/home/toscan/ToscanWorkspace/orchemist-skills/skills/orchemist-run.md` (prose rules, lines ~230–250)
  - Describes verdict extraction algorithm in plain English
  - References engine's `verdict_parser.extract_verdict()` contract but implementation diverges
  - README.md (line ~40) acknowledges: "Verdict extraction follows the engine's `verdict_parser.extract_verdict` contract, but the implementation is in a skill's prose, not a parser library — **corner cases may differ**"

**Why it's a duplicate:**
- Both `verdict_parser.extract_verdict()` and `review_parser.extract_verdict()` solve the same problem: extracting a boolean verdict from LLM text output
- Input shapes differ slightly (verdict_parser accepts file paths; review_parser doesn't)
- **Output case differs** — verdict_parser returns lowercase; review_parser returns uppercase
- review_parser has moved to a more aggressive 4-layer cascade (layers 0–3) while verdict_parser stays at 2-pass
- skills markdown prose rules are duplicating the contract, creating opportunity for divergence during updates
- Skills README explicitly admits the corner-case drift risk

**Consolidation proposal:**
1. Merge the two functions into a single `extract_verdict()` in `verdict_parser.py` that:
   - Accepts both text and optional file paths (as it currently does)
   - Adds optional `mode` parameter: `"verdict_parser"` (current 2-pass behavior, backward-compat) or `"review_parser"` (4-layer cascade)
   - Unifies output case: always return lowercase (matches TaskResult verdicts and enum values in schemas.py)
   - Bump the exported API in transitions.py to re-export from verdict_parser only
2. Delete `review_parser.extract_verdict()` and all private helpers (`_smart_full_text_scan`, `_tail_weighted_scan`, `_haiku_extraction`)
3. Update `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/daemon.py` (line ~850) to use the merged function
4. Document the prose rules in orchemist-skills/skills/orchemist-run.md with a **GROUND TRUTH** reference to the single engine function (add a code block with signature + example)

**Risk / cost to consolidate:**
**HIGH** (3–4 days implementation + test iteration). review_parser.extract_verdict() is called by:
- `parse_review_output()` in review_parser.py itself (lines 494–498)
- daemon.py (conditional import, fallback path when review verdict extraction is needed)
- Not directly imported in sequencer or acceptance_test_adversary — they use verdict_parser instead

Risks:
- review_parser's 4-layer cascade is more aggressive (catches tail-end verdicts LLM authors didn't lead with); removing it may regress some edge cases
- Case unification (lowercase) requires auditing all callsites that check `verdict == "APPROVE"` (likely in daemon.py)

**Open issue / PR reference:**
#687 — "review_parser.extract_verdict() diverges from shared verdict_parser — parallel implementation drift risk"

---

## Group 2: File Hash Computation — Three Private Implementations (High DRY Violation)

**Locations:**
- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/file_guard.py` — `compute_hash()` (lines 31–49)
  - Public API for SHA256 file hashing
  - Reads file in 65536-byte chunks; returns hex digest
  - Used by sequencer to verify protected files between phases

- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/validator_runner.py` — `_compute_hash()` (private function)
  - Identical implementation: reads in same chunk size, returns hexdigest
  - No docstring; function name prefixed with `_` (private)

- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/test_store.py` — `_compute_hash()` (private method, inside a class)
  - Nearly identical; slightly more detailed docstring
  - Same 65536-byte chunk read pattern

**Why it's a duplicate:**
- All three are functionally identical SHA256 hash-on-file implementations
- file_guard.py exports the canonical public function but validator_runner.py and test_store.py have no incentive to import it (internal use only)
- Three independent implementations create future maintenance burden (e.g., if a security fix is needed, it must be applied in triplicate)

**Consolidation proposal:**
1. Keep the public `compute_hash()` in file_guard.py (it's the primary API)
2. In validator_runner.py and test_store.py, replace private `_compute_hash()` implementations with imports:
   ```python
   from .file_guard import compute_hash
   ```
3. Remove the private function definitions; replace calls to `_compute_hash(path)` with `compute_hash(path)`
4. No behavioral change; code becomes 6 lines shorter in two files

**Risk / cost to consolidate:**
**LOW** (1 hour). Changes are purely mechanical; test coverage already exists for file_guard.compute_hash().

**Open issue / PR reference:**
None identified; this is a common pattern (private reimplementation to avoid coupling).

---

## Group 3: Slugify Functions — Two Within-Engine + One Cross-Language (Medium Consolidation Burden)

**Locations:**
- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/issue_automation.py` — `slugify_branch()` (lines 220–260)
  - Converts issue title → git-branch-safe slug (40 chars default)
  - NFKD normalization → Unicode to ASCII transliteration → lowercase → hyphens → trim

- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/importers/plugin_command.py` — `slugify()` (private function)
  - Converts text → URL-safe slug
  - Similar algorithm but no max-length constraint
  - Used by plugin-command importer to generate unique IDs

- `/home/toscan/ToscanWorkspace/orchemist-ide/extensions/markdown-language-features/src/slugify.ts` — `githubSlugifier` (complex interface + implementation)
  - TypeScript port of GitHub's markdown heading slugification
  - Different purpose (heading IDs) but same core algorithm
  - 100+ lines of regex magic for Unicode stripping

**Why it's a duplicate:**
- `slugify_branch()` and `slugify()` are nearly identical in algorithm; both solve "convert prose to hyphenated slug"
- Difference: `slugify_branch()` is max_length-constrained; `slugify()` is not
- TypeScript version (orchemist-ide) duplicates the core algorithm in a different language
- Issue #511 reference in issue_automation.py suggests this was known; slugify_branch was added as a git-specific variant
- No shared library for slugification across engine

**Consolidation proposal:**
1. Merge `slugify()` and `slugify_branch()` in the Python engine into a single `slugify()` function with optional `max_length` parameter:
   ```python
   def slugify(text: str, max_length: Optional[int] = None) -> str:
       """Convert text to a hyphenated slug.
       
       Args:
           text: Raw text (may contain Unicode, spaces, punctuation).
           max_length: Optional max length. If None, no truncation.
       
       Returns:
           Lowercase, hyphenated slug safe for URLs and git branch names.
       """
   ```
2. Update issue_automation.py to use `slugify(..., max_length=40)` instead of `slugify_branch()`
3. For orchemist-ide's TypeScript version: leave as-is (it's a VS Code built-in extension fork; scope is UI markdown rendering, not shared code)

**Risk / cost to consolidate:**
**MEDIUM** (4–8 hours). 
- Must verify `plugin_command.py` usage (likely low-traffic; plugin importer may be legacy)
- Test both code paths after merge
- orchemist-ide slug function is out of scope (separate codebase, different purpose)

**Open issue / PR reference:**
#511 (implied from docstring; "slugify_branch — convert a title to a git-branch-safe slug (Issue #511)")

---

## Group 4: Retry/Backoff Configuration — Scattered Definitions (Low Severity, High Risk if Missed)

**Locations:**
- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/schemas.py` — `calculate_retry_delay()` (lines 414–420)
  - Exponential backoff: `base_delay * 2^(attempt - 1)`, capped at 60 seconds
  - Used by task queue

- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/recovery.py` — `get_retry_config()` method
  - Task-type-specific retry configuration (max retries per TaskType)
  - References `DEFAULT_MAX_RETRIES` in schemas.py

**Why it's a duplicate / fragmentation risk:**
- Retry logic is split between schemas.py (backoff timing) and recovery.py (config per task type)
- `DEFAULT_MAX_RETRIES` dict in schemas.py (lines 447–461) hardcodes retry limits per TaskType
- recovery.py likely re-implements or wraps this; no single source of truth
- If backoff formula needs adjustment, two or more places must be updated

**Consolidation proposal:**
1. Consolidate all retry config into a new `RetryPolicy` class in schemas.py:
   ```python
   @dataclass
   class RetryPolicy:
       max_retries: int
       base_delay_seconds: int = 1
       max_delay_seconds: int = 60
       backoff_multiplier: float = 2.0
   ```
2. Keep `DEFAULT_MAX_RETRIES` dict but have it reference RetryPolicy objects
3. Update recovery.py to use RetryPolicy from schemas.py instead of duplicating logic

**Risk / cost to consolidate:**
**LOW** (2–3 hours). Largely refactoring; no behavior change required.

**Open issue / PR reference:**
None identified; internal cleanup opportunity.

---

## Group 5: Git Operations Modules — Intentional Separation, Fragile Boundary (Medium Coordination Risk)

**Locations:**
- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/git_integration.py` (1310 lines)
  - GitContext class: feature-branch lifecycle (create → stage/commit → diff → push → merge gate)
  - Orchestrator-level responsibility; runs in sequencer process

- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/git_handoff.py` (309 lines)
  - GitHandoff class: spec-loop phase-output commit tracking
  - Temporary local branches (spec-loop/{run_id}); no push
  - Isolated per-phase snapshot capability

**Why it's a potential duplicate / fragmentation risk:**
- **Not a strict duplicate**, but two separate git management systems for different purposes
- Both define `_git()` wrapper method (subprocess.run with git commands)
- Both enforce no `--force` push restrictions
- Different command sets; boundaries are clear (feature branch vs. temp spec loop)
- **Fragile boundary**: future git-related features might belong in either module, leading to inconsistent patterns

**Consolidation proposal:**
1. **Do NOT merge** (boundaries are intentional and serve different phases)
2. **DO extract shared utilities** into a new `git_utils.py`:
   - `GitCommandRunner` class encapsulating `subprocess.run` wrapper with safety checks
   - Shared config (no-force enforcement, error handling patterns)
3. Have both git_integration.py and git_handoff.py import and use GitCommandRunner

**Risk / cost to consolidate:**
**MEDIUM** (6–8 hours). Extraction is low-risk but requires test updates for both modules.

**Open issue / PR reference:**
None identified; architectural cleanup.

---

## Group 6: Python Enums / TypeScript Types — Frontend ↔ Backend Type Mismatch (Medium Sync Burden)

**Locations:**
- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/schemas.py`
  - `TaskState` enum (lines 44–52): `QUEUED`, `RUNNING`, `SUCCESS`, `FAILED`, `RETRY`, `PERMANENTLY_FAILED`, `CANCELLED`
  - `RunStatus` implied in schemas but **NOT explicitly defined** as enum (values appear in docstrings and TaskResult fields)
  - `ModelTier` enum (lines 72–76): `HAIKU`, `SONNET`, `OPUS`

- `/home/toscan/ToscanWorkspace/orchemist/frontend/lib/types.ts`
  - `RunStatus` type alias (lines 78–86): `'pending' | 'running' | 'success' | 'failed' | 'cancelled' | 'crashed' | 'scoring_failed'`
  - **Mismatch**: Backend has no `'pending'`, `'crashed'`, or `'scoring_failed'` in TaskState enum
  - No explicit ModelTier type in frontend (values are free-form strings in TemplateSummary)

**Why it's a duplicate / fragmentation risk:**
- Frontend uses a **different** RunStatus vocabulary than backend TaskState
- TypeScript types are hand-maintained mirrors of Python Pydantic enums; divergence is easy
- No code generator or shared schema; every enum change requires manual sync in two files
- Frontend's `scoring_failed` status has no backend counterpart

**Consolidation proposal:**
1. Add missing Python enum to schemas.py:
   ```python
   class RunStatus(str, Enum):
       RUNNING = "running"
       SUCCESS = "success"
       FAILED = "failed"
       CANCELLED = "cancelled"
   ```
2. Update FastAPI response models (in web/api.py if it exists) to use RunStatus instead of free-form strings
3. Regenerate TypeScript types using a code generator (e.g., Pydantic's JSON schema export + ts-json-schema-generator)
4. Or: document the enum differences in a shared CONTRACT file (e.g., `docs/enum-compatibility.md`)

**Risk / cost to consolidate:**
**HIGH** (5–7 days). Requires backend API changes, frontend regeneration, and testing across web layer.

**Open issue / PR reference:**
None identified; implicit from ReviewResult / ReviewOutcome work (schemas.py was recently updated; frontend may be out of sync).

---

## Group 7: Verdict Format Contract — Prose vs. Code Divergence in Skills (Low Severity, Maintenance Burden)

**Locations:**
- `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/verdict_parser.py` — `extract_verdict()` contract (lines 48–92)
  - Structured markdown: `"VERDICT: <keyword>"` lines (Pass 1)
  - Fallback regex matching bare keywords (Pass 2)
  - Documented via docstring + regex comments

- `/home/toscan/ToscanWorkspace/orchemist-skills/skills/orchemist-review.md` (lines ~30–50)
  - Prose instructions for LLM reviewers
  - States: "Your response MUST start with one of these verdicts on the very first line: `APPROVE`, `REQUEST_CHANGES`, or `ABORT`"
  - **Does NOT mention** the VERDICT: prefix syntax or markdown-stripping fallback logic
  - Simpler contract than actual parser supports

- `/home/toscan/ToschemWeekst/orchemist-skills/skills/orchemist-adversary.md` (lines ~90–105)
  - Similar prose instructions for adversary output format
  - Tells agents to emit verdict on first line only; does NOT mention two-pass logic

**Why it's a duplicate / divergence risk:**
- Skills markdown describes a **simpler contract** than the actual engine parser supports
- LLM agents in skills are told to put verdict on the first line; they don't know about the markdown-stripping fallback (Pass 2)
- If engine parser is updated (e.g., to support `**APPROVE**` in bold), skills markdown won't automatically reflect it
- Prose rules are the "spec" agents see; engine code is the "implementation"; divergence is a ground-truth risk

**Consolidation proposal:**
1. Keep the simple prose contract in orchemist-review.md and orchemist-adversary.md (agents need simple, actionable rules)
2. In orchemist-skills/README.md or agents/orchemist-adversary.md, add a **GROUND TRUTH POINTER** section:
   ```markdown
   ## Verdict Extraction Contract (GROUND TRUTH)
   
   The orchestrator parses your verdict output using the engine's
   `verdict_parser.extract_verdict()` function. For details on the full
   extraction algorithm (including markdown fallback, case-insensitivity,
   and priority ordering), see:
   
   **Engine source:** `src/orchestration_engine/verdict_parser.py`
   
   Quick summary:
   - First line preferred: bare APPROVE, REQUEST_CHANGES, or ABORT
   - Fallback: any of these keywords anywhere in output (case-insensitive)
   - Markdown noise (bold, backticks, etc.) is stripped
   ```
3. Link from skill markdown to this ground truth reference
4. Add a test in `tests/test_verdict_parser.py` (if not already present) that runs the exact skill examples through the parser to ensure they pass

**Risk / cost to consolidate:**
**LOW** (2 hours). Documentation-only change; no code modification.

**Open issue / PR reference:**
None identified; documentation improvement.

---

## Categories breakdown

| Category | Count | Severity |
|----------|-------|----------|
| Verdict extraction (verdict_parser ↔ review_parser ↔ skills prose) | 1 | **HIGH** — Shipping bug risk (divergent implementations) + drift risk (#687 documented) |
| File hash computation (file_guard ↔ validator_runner ↔ test_store) | 1 | **MEDIUM** — DRY violation + future maintenance burden if security fix needed |
| Slugify functions (issue_automation ↔ plugin_command ↔ orchemist-ide TypeScript) | 1 | **MEDIUM** — Code duplication; cross-language version out of scope |
| Retry/backoff config (schemas.py ↔ recovery.py) | 1 | **LOW** — DRY violation but low risk; mostly refactoring |
| Git operations (git_integration ↔ git_handoff) | 1 | **LOW** — Intentional separation; fragile boundary suggests utility extraction |
| Enum/type mismatch (schemas.py TaskState/ModelTier ↔ frontend types.ts RunStatus) | 1 | **HIGH** — Sync burden; frontend and backend vocabularies diverge |
| Verdict format contract (verdict_parser.py code ↔ skills markdown prose) | 1 | **MEDIUM** — Prose rules are simpler than engine supports; ground-truth pointer needed |
| **TOTAL** | **7** | |

---

## Recommended shared-library structure

### 1. **parsing_utils.py** (new shared module)
**Path:** `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/parsing_utils.py`

**Purpose:** Consolidate verdict extraction and output parsing.

**Moves here:**
- `verdict_parser.extract_verdict()` (merged with review_parser logic; add `mode` parameter)
- Private helpers: `_pass1()`, `_pass2()` (from verdict_parser)
- Phase outcome utilities: `determine_outcome()` (currently in transitions.py, re-export)

**Callers:**
- sequencer.py, transitions.py, acceptance_test_adversary.py, spec_adversary.py, daemon.py, adversary_parser.py

**Why:** Centralizes all "extract structure from LLM text" logic; single source of truth for verdict extraction contract.

---

### 2. **file_utils.py** (new shared module)
**Path:** `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/file_utils.py`

**Purpose:** Common file operations (hashing, checksums, guards).

**Moves here:**
- `file_guard.compute_hash()` (public; already exported)
- `file_guard.compute_directory_hash()` (public; already exported)
- Remove `_compute_hash()` from validator_runner.py and test_store.py; add imports instead

**Callers:**
- file_guard.py (already the primary module), validator_runner.py, test_store.py, sequencer.py

**Why:** Eliminates three private reimplementations; centralize all crypto operations.

---

### 3. **git_utils.py** (new shared module)
**Path:** `/home/toscan/ToscanWorkspace/orchemist/src/orchestration_engine/git_utils.py`

**Purpose:** Shared git command execution and safety checks.

**Moves here:**
- `GitCommandRunner` class (new extraction from git_integration.py and git_handoff.py)
- Shared `_git()` wrapper method with subprocess safety checks
- Force-push rejection logic
- Error handling patterns for common git failures

**Callers:**
- git_integration.py, git_handoff.py

**Why:** Avoids duplicate safety-check implementations; future git-related code can use the same runner.

---

## Priority actions (phased consolidation)

1. **Phase 1 (Critical):** Merge verdict_parser and review_parser
   - Resolves #687 (documented issue)
   - Unblocks downstream agents and pipeline from divergence risk
   - Time: 3–4 days

2. **Phase 2 (High):** Deduplicate file hash functions
   - Mechanical; low risk
   - Time: 1 hour

3. **Phase 3 (Medium):** Merge slugify functions + extract git utilities
   - Time: 1–2 days

4. **Phase 4 (Medium):** Sync enum definitions (Python ↔ TypeScript)
   - Largest scope; requires API coordination
   - Time: 5–7 days

5. **Phase 5 (Low):** Document verdict contract in skills; add ground-truth pointers
   - Time: 2 hours (documentation only)

---

## Recommendations for future prevention

1. **Enforce Phase 0 inventory at PR time:** Add a CI check that fails if a PR modifies multiple files with identical code patterns (e.g., hash functions, verdict extraction). Suggest consolidation as a PR comment.

2. **Code duplication threshold:** Establish a rule: functions longer than ~20 lines should not be reimplemented. Require import + reuse instead.

3. **Enum/type sharing:** Auto-generate TypeScript types from Python Pydantic schemas using a code generator (e.g., datamodel-code-generator). Commit the generated types; update on every schemas.py change.

4. **Shared-library ownership:** Assign a code owner (e.g., @rene for Orchemist) to critical modules like verdict_parser.py, file_guard.py, git_integration.py. Require approval before changes to prevent silent divergence.

5. **Skills markdown contracts:** Add a `GROUND_TRUTH` section pointing to engine code. Validate in tests (e.g., run skill examples through parser to ensure they pass).

