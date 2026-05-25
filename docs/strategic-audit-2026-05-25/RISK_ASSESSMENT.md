# RISK-ASSESSMENT AUDIT REPORT — 2026-05-25

## TL;DR (3-line summary)

Three **current** risk documents are maintained (AUTONOMY.md, ROADMAP.md, post-remediation advisory) with mutually consistent claims about branch protection, budget controls, and autonomy bottlenecks. AUTONOMY.md's core claim (unprotected `main` branches on all 4 repos as of 2026-05-24 19:31 UTC) is now partially outdated: ruleset 16835594 was applied to `orchemist/main` on 2026-05-24 and a matching ruleset was applied to `orchemist-skills/main`. The remaining gap is that several critical risks are not documented anywhere: PyPI Trusted Publisher trust chain, operator-edited `admin.json`, SQLite WAL multi-writer assumptions, feature-flag/runtime divergence, and SSE resource exhaustion.

---

## Document Inventory

| Path | Type | Last Update | Status |
|---|---|---|---|
| `docs/harness-redesign-2026-05-24/AUTONOMY.md` | Current | 2026-05-24 19:31 UTC | Active investigation report — branch-protection audit with proposed rulesets |
| `SECURITY.md` | Policy | (no date) | Reporting/response SLA only — not risk inventory |
| `ROADMAP.md` | Current | 2026-03-11 | Active roadmap with "Risks & Guardrails" section (§645–678) |
| `docs/internal/level5-requirements.md` | Current | 2026-03-02 | Phase 2/3/4 technical specs with embedded TR-level risk callouts |
| `docs/internal/forensics_2026-03-13/09-post-remediation-advisory.md` | Historical (reference) | 2026-03-13 | Remediation summary + 10 operational/maintenance risks for first deployment |
| `docs/internal/forensics_2026-03-13/` (full set) | Historical audit | 2026-03-13 | 8-part forensic review; read-only reference, not normative |

---

## Verified-Current Claims

- **AUTONOMY.md §1**: Branch protection — partially superseded. `orchemist` ruleset 16835594 active on `main` as of 2026-05-24; matching ruleset applied to `orchemist-skills`. `orchemist-website` and `orchemist-ide` still unprotected (latter is being sunset; former needs ruleset).

- **ROADMAP.md §669**: "No force push. Ever." — VERIFIED ENFORCED. Code guard in `git_integration.py` throws `ValueError` if `--force` detected in any git push command.

- **Post-remediation advisory §1**: Budget controls mandatory to prevent runaway spend. VERIFIED CORRECT: `budget:` YAML block and `daily_budget_cap_usd` are the documented throttles.

- **Post-remediation advisory §2**: "GitHub CLI must be authenticated." VERIFIED.

- **Post-remediation advisory §4**: SQLite single-server, no automated backup. VERIFIED: `.orchestration-engine/engine.db` is the sole DB; WAL mode enabled but no backup mechanism.

- **Post-remediation advisory §5**: Daemons are fire-and-forget; orphaned runs remain `status='running'`. VERIFIED via #754 (zombie runs).

- **Post-remediation advisory §6**: Retry system can compound costs. VERIFIED: `adaptive_retry.py` escalates Haiku→Sonnet→Opus.

---

## Outdated / Contradicted Claims

- AUTONOMY.md §1 branch-protection bullet is now partially out of date (`orchemist`, `orchemist-skills` protected; `orchemist-website` still open). Action: re-publish AUTONOMY.md with current state in next audit cycle.

---

## Risks NOT in Any Doc (Recommend Filing)

1. **PyPI Trusted Publisher Trust Chain (PKI Risk)** — HIGH. Tag-triggered OIDC release; any merger can tag `v*.*.*`. No SOP for version bumps or tag creation. → File issue: "PKI: Document and enforce release approval SOP (require PR review + signed tags for releases)."

2. **Operator-Edited `admin.json` Surface** — MEDIUM. PR #828 introduced `~/.orchestration-engine/admin.json` as a writable config surface; controls autonomy level + feature flags. No validation pipeline, no audit log, no permissions doc. → File issue: "CONFIG: Document `~/.orchestration-engine/` files and add audit log for admin.json edits."

3. **SQLite WAL Multi-Writer Assumptions** — MEDIUM. Multiple daemon processes may write concurrently. No queue depth monitoring, no backpressure. → File issue: "DATABASE: Add concurrent-daemon backpressure to prevent SQLite WAL contention."

4. **Feature Flags Don't Affect Runtime** — MEDIUM. Admin UI writes feature_flags but no sequencer/daemon code reads them. UI is a false control. → File issue: "FEATURES: Wire admin feature_flags into runtime; add cache TTL strategy."

5. **SSE Long-Lived Connection Resource Exhaustion** — LOW. No max-connections, no idle timeout, no rate limit on `/api/v1/runs/{id}/events`. → File issue: "INFRA: Add SSE connection limits and per-IP rate limiting."

6. **Path-Traversal Guards on Artifact Endpoints** — HIGH (status unknown). New `/api/v1/runs/{id}/artifacts/*` endpoints (PR #825/#828) — must verify canonical-path enforcement. → File issue: "SECURITY: Audit artifact endpoints for path traversal."

---

## Recommended Next Actions

1. **Immediate (this sprint):**
   - Republish AUTONOMY.md with current branch-protection state.
   - File issues 1–6 above.
   - Manual audit of artifact endpoint path validation (PR #825/#828).

2. **Before Level 5 promotion:**
   - Wire feature_flags into runtime (or remove from admin UI).
   - SQLite queue depth monitor.
   - admin.json schema validation + audit log.

3. **Security (60 days):**
   - Document release SOP + tag-signing policy.
   - SSE rate limiting.

---

## Cross-References

- AUTONOMY.md: `docs/harness-redesign-2026-05-24/AUTONOMY.md`
- ROADMAP.md: `ROADMAP.md` (§645–678 guardrails)
- Post-remediation advisory: `docs/internal/forensics_2026-03-13/09-post-remediation-advisory.md`
- Git force-push guard: `src/orchestration_engine/git_integration.py`

---

*Audit completed 2026-05-25. No file modifications made.*
