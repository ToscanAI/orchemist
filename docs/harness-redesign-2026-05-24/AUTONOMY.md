# Autonomy Posture — preconditions to operate at Level 5

> **Audience:** René. **Date:** 2026-05-24. **Status:** Investigation report — no destructive actions taken yet.
>
> This document reports what is currently true about the four Orchemist repos with respect to branch protection, push policy, and the autonomy surface needed to safely run pipelines unattended. The harness redesign epic depends on these being addressed.

---

## 1. Branch protection — none of the four repos has it

Checked via `gh api repos/<owner>/<repo>/branches/main/protection` on 2026-05-24 19:31 UTC.

| Repo | Required reviews | Required status checks | Force-push block | Restrict push | Result |
|---|---|---|---|---|---|
| `ToscanAI/orchemist` | none | none | none | none | **HTTP 404 — "Branch not protected"** |
| `ToscanAI/orchemist-skills` | none | none | none | none | **HTTP 404 — "Branch not protected"** |
| `ToscanAI/orchemist-website` | none | none | none | none | **HTTP 404 — "Branch not protected"** |
| `ToscanAI/orchemist-ide` | none | none | none | none | **HTTP 404 — "Branch not protected"** (slated for deprecation per pivot) |

**Why this matters for autonomy.** The engine refuses to force-push internally (`git_integration.py`), but the harness will be issuing `git push` operations from automation. Without branch-protection rules at the GitHub layer, a buggy fix-pipeline that does an aggressive cleanup commit could blow away `main` directly, with no second line of defence. At Level 5 the human review surface is "exceptions only" — and unprotected `main` is a non-trivial exception class to eliminate.

**Recommended minimum protection** to unlock Level 5 promotion on `ToscanAI/orchemist`:
- Require pull request before merging — **1 review** (acceptable: PR author = pipeline, reviewer = trust-engine APPROVE verdict via status check)
- Require status checks to pass before merging — `CI`, `acceptance-tests`
- Require linear history — **off** (we squash-merge by convention; linear history is enforced by the merge mode, not this flag)
- Block force-push — **on**
- Block deletion — **on**
- Restrict who can push — not yet (we want pipelines to push branches; review gate covers the merge step)

Equivalent rules for `orchemist-skills` and `orchemist-website`, omitting `acceptance-tests` (those repos do not run the engine's pytest suite).

`orchemist-ide` — per the pivot memory, this repo is being **dropped**. Branch protection is not needed; instead, archive the repo or add a deprecation notice to its README (separate harness-deprecation epic).

## 2. Push pattern audit

Both engine and skills work today by:
- Engine pipelines (when run via `orch run --mode openrouter`) → push feature branches with the pipeline's gh CLI calls → open PR via `gh pr create` → human reviewer = René.
- Skills pack (`/orchemist:run` in Claude Code) → push feature branches by the local skill `orchemist-implementer` → open PR.

Both paths *create* branches and PRs but do not auto-merge today; auto-merge gating lives in the engine's `routing.py` (confidence-thresholded). Once branch protection is enabled, the auto-merge path will require the protection's required-checks to be green — which closes the loop nicely.

## 3. What I have NOT done (and why)

I have **not** enabled branch protection autonomously on any repo. Branch protection is a privileged-org change that affects every contributor — adding it midway through an in-flight branch (e.g. `chore/bump-0.9.0`, the engine's current local branch) could surprise concurrent work. The recommendation lives in the harness epic; the actual `gh api -X PUT repos/.../branches/main/protection` call is one short script that you can run after acknowledging the proposed rule set.

I have **not** changed any default branch, archived `orchemist-ide`, or modified any GitHub-side configuration.

## 4. Autonomy posture summary (engine bottlenecks for Level 5)

From `ROADMAP.md` §"What's Missing for Full Autonomy" cross-referenced with the duplicate audit + frontend audit:

| Bottleneck | What unblocks it | Owner artifact |
|---|---|---|
| Fleet monitoring dashboard (3.4) | Harness UI screen 1 + multi-repo orchestration (4.6 / Sprint 12) | This epic |
| Stale detection / proactive maintenance (3.5) | New `stale_scanner.py` module + scheduled trigger | Sub-issue |
| Branch protection drift on `main` | Org admin enables rules on 3 repos | This document, §1 |
| Frontend ↔ backend enum drift (`RunStatus` / `TaskState`) | Generate TS types from Pydantic at build time | Sub-issue from duplicate audit |
| Verdict parser duplication (`#687`) | Consolidate `review_parser.extract_verdict` into `verdict_parser` | Sub-issue from duplicate audit |
| Gates UI missing | Wire existing `/api/v1/gates` endpoints to a `Trust & Gates` page | Harness screen 4 |
| Dialogue phase (Track B / PR #808) | Land #808 to `main` after open question 3 is answered | Pipeline PR |
| Orchemist-IDE deprecation | Archive repo + README redirect to harness | Separate epic |

**Reading guide.** The "Open decisions" section of `VISION.md` (§7) lists the five decisions that block kicking off the harness build. Branch protection is decision-independent — it can be enabled today regardless of how the open decisions land.

---

## Appendix A — proposed branch-protection script

When ready (after your acknowledgement), enabling protection on the three live repos is one parametrised loop:

```bash
for repo in orchemist orchemist-skills orchemist-website; do
  gh api -X PUT "repos/ToscanAI/${repo}/branches/main/protection" \
    -F required_pull_request_reviews.required_approving_review_count=1 \
    -F required_pull_request_reviews.dismiss_stale_reviews=true \
    -F enforce_admins=false \
    -F allow_force_pushes=false \
    -F allow_deletions=false \
    -F required_status_checks.strict=true \
    -F 'required_status_checks.contexts[]=CI'
done
```

Add `required_status_checks.contexts[]=acceptance-tests` for `orchemist` only. `enforce_admins=false` keeps the admin (you) able to push directly through the gate when needed for emergency hot-fixes; flip to `true` once the pipeline + reviewer combo is trusted.
