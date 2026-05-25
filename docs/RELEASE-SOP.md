# Orchemist Release SOP

Standard operating procedure for tagging + publishing a new `orchemist`
release to PyPI. Closes #837 — addresses risk #1 from the
[2026-05-25 strategic audit](./strategic-audit-2026-05-25/RISK_ASSESSMENT.md)
(PyPI Trusted Publisher trust chain).

---

## 1. Who can release

Only maintainers listed in `MAINTAINERS.md` may tag a release. The
release workflow has no GitHub-Actions-level branch restriction — the
PyPI **Trusted Publisher** at <https://pypi.org/manage/account/publishing/>
gates publication on `repo == ToscanAI/orchemist AND workflow ==
publish.yaml`. The branch and tag check is enforced by this SOP, not by
CI.

---

## 2. Pre-release checklist

Before opening a release PR:

- [ ] All PRs intended for this release are merged into `main`.
- [ ] `main` CI is green for the latest commit.
- [ ] `pyproject.toml` `version` matches the intended release number.
- [ ] `CHANGELOG.md` `[Unreleased]` section is populated with everything
      since the previous release; a dated `[X.Y.Z] - YYYY-MM-DD` heading
      is added below.
- [ ] No `## [Unreleased]` section is left empty after moving its bullets
      to the dated section.
- [ ] No outstanding security findings flagged in `SECURITY.md` or in
      any `[BLOCKER]` issue label.
- [ ] Branch protection ruleset on `main` is **active** (verify
      `gh api repos/ToscanAI/orchemist/rulesets/16835594` returns
      `enforcement: active`).

If any item is unchecked, do not proceed.

---

## 3. Version-bump PR

1. Create a branch: `release/vX.Y.Z`.
2. Bump `pyproject.toml` `version = "X.Y.Z"` and move the
   `CHANGELOG.md [Unreleased]` block under `## [X.Y.Z] - YYYY-MM-DD`.
3. Open a PR titled `Release vX.Y.Z`.
4. **Require two maintainer reviews** before merge. The release PR is
   the audit trail for what shipped under this version number; a single
   approver creates a single point of failure for accidental or coerced
   releases.
5. Merge via squash; do **not** force-push the release branch.

The merged commit message of the release PR becomes the GitHub release
notes (see step 5).

---

## 4. Tagging

Once the release PR merges to `main`:

1. Pull `main` locally and verify the commit you intend to tag is on it:

   ```bash
   git fetch origin
   git checkout main
   git pull --ff-only
   git log --oneline -5  # confirm the release PR's squash commit
   ```

2. Create a **signed** tag:

   ```bash
   git tag -s vX.Y.Z -m "Release vX.Y.Z"
   ```

   If `git tag -s` fails with "gpg failed to sign" or "no secret key",
   configure `user.signingkey` first (see <https://docs.github.com/en/authentication/managing-commit-signature-verification/telling-git-about-your-signing-key>);
   never push an unsigned release tag.

3. Push the tag:

   ```bash
   git push origin vX.Y.Z
   ```

   The push triggers `.github/workflows/publish.yaml` which builds the
   wheel, runs `twine check`, and publishes to PyPI via Trusted
   Publisher OIDC.

4. Monitor the workflow at
   <https://github.com/ToscanAI/orchemist/actions/workflows/publish.yaml>.
   If it fails, do **not** retag — open a fix PR, bump the patch
   version, and start again. Republishing a yanked or failed version
   number to PyPI is not supported.

---

## 5. Post-release

1. Verify the package is live: `pip install --no-cache-dir orchemist==X.Y.Z`
   on a fresh machine or virtualenv.
2. Create a GitHub release at
   <https://github.com/ToscanAI/orchemist/releases/new?tag=vX.Y.Z> using
   the merged release PR's commit message as the body.
3. Announce in `#orchemist-releases` (when that channel exists) or
   equivalent.

---

## 6. What is enforced by CI vs by this document

| Check | Enforced by | Notes |
|---|---|---|
| Trusted Publisher identity (repo + workflow) | PyPI | Cannot be bypassed — PyPI rejects publishes from any other source |
| `twine check dist/*` passes before publish | `publish.yaml:38` | Fails the workflow on malformed metadata |
| Test suite green on `main` | `ci.yml` push trigger | Required to consider `main` ready to tag |
| 2-reviewer rule on release PRs | This SOP | Convention; not currently CI-enforced — see issue #837 follow-up |
| Tag signing | This SOP | Convention; not currently CI-enforced — see issue #837 follow-up |
| Tag created from `main` | This SOP | Convention; the workflow is branch-agnostic by design (allows hotfix-branch releases when needed) |

The "Convention" rows are gaps a future CI hardening PR should close —
see the open follow-ups below.

---

## 7. Follow-up hardening (open work)

Tracked but not yet implemented:

- **GH Actions check that flags PRs touching `pyproject.toml` `version`
  without a `release:` label or 2 reviews.** Today the 2-reviewer
  requirement is honour-system.
- **Tag-signature enforcement in `publish.yaml`.** Today the workflow
  publishes any pushed `v*.*.*` tag regardless of signature. A future
  step should run `git tag -v $GITHUB_REF_NAME` and fail closed.
- **Branch-protection ruleset on `main` requiring signed commits.**
  Currently the ruleset (id 16835594) blocks force-push but does not
  require signatures.
- **Automated release-notes generation** from CHANGELOG (or `gh release
  create --generate-notes`) instead of manual copy-paste in step 5.2.

When closing each follow-up, update the table in §6 from "Convention"
to "CI" and remove the corresponding line here.

---

## 8. Incident response

If a bad release ships:

1. **Yank** the version on PyPI: <https://pypi.org/manage/project/orchemist/release/X.Y.Z/>
   → `Yank release`. Existing installs continue working; new
   installs of `==X.Y.Z` will fail with a clear message.
2. Open a **post-incident** issue describing what happened, why it
   wasn't caught by CI / review, and the structural fix.
3. Ship a patch release `X.Y.Z+1` with the fix. Do NOT republish a new
   wheel under the yanked version number.
4. Update this SOP with whatever check would have caught the incident.

---

*Last reviewed: 2026-05-25 (closes #837).*
