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

   The release-signing key is already configured on the release host
   (ed25519, fpr `E3A2819B37178D9844E45AD1FD72AF771848CF42`, passphraseless;
   public key committed at `.github/release-signing-pubkey.asc`). The
   `orchemist` repo sets `tag.gpgsign=true`, so tags created there are signed
   automatically; `-s` makes it explicit.

   **CI enforces this FAIL-closed (#890):** `publish.yaml` imports
   `.github/release-signing-pubkey.asc` and runs `git tag -v` — an unsigned tag,
   or one signed by a key not in that file, **blocks the publish**. Never push
   an unsigned release tag.

   If `git tag -s` fails with "no secret key" you are on a host without the
   private key: sign on the release host, or add the new signer's public key to
   `.github/release-signing-pubkey.asc` (append its armored block) and commit it
   before tagging. Caveat: a passphraseless key co-located with release
   automation proves *provenance* (the tag came from a host holding the key),
   not independent human authorization — it is comparable in trust to the
   Trusted-Publisher repo/workflow binding, not stronger.

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
| `twine check dist/*` passes before publish | `publish.yaml` | Fails the workflow on malformed metadata |
| Test suite green on `main` | `ci.yml` push trigger | Required to consider `main` ready to tag |
| 2-reviewer rule on release PRs (incl. `pyproject.toml` bumps) | CI | `.github/CODEOWNERS` requires maintainer approval; combined with branch protection's "1 review required" this yields 2 reviewers (closes #890) |
| Tag signing | CI (WARN; FAIL gate 2026-06-03) | `publish.yaml` runs `git tag -v $GITHUB_REF_NAME`; currently warns only. A follow-up PR (#890 follow-up) imports the maintainer pubkey and flips the step to FAIL closed on 2026-06-03 |
| Tag created from `main` | Convention | Workflow is branch-agnostic by design (allows hotfix-branch releases when needed) |
| Signed commits on `main` | Convention | Branch-protection ruleset 16835594 admin update pending — out of scope for the #890 code PR |

The "Convention" rows are gaps a future CI hardening PR should close —
see the open follow-ups below.

---

## 7. Follow-up hardening (open work)

Closed in #890:

- ~~**GH Actions check that flags PRs touching `pyproject.toml`
  `version` without a `release:` label or 2 reviews.**~~ Replaced
  with `.github/CODEOWNERS` requiring maintainer approval on
  `pyproject.toml` (the simpler standard mechanism). Combined with
  branch protection this is the 2-reviewer requirement.
- ~~**Tag-signature enforcement in `publish.yaml`.**~~ Added as a
  `git tag -v` step in WARN mode. A separate follow-up PR will
  import the maintainer pubkey and flip to FAIL mode on 2026-06-03.

Still open:

- **WARN → FAIL transition for the `git tag -v` step.** Scheduled
  for 2026-06-03; requires the maintainer GPG public key to be
  available to the runner via a secret (e.g.
  `MAINTAINER_GPG_PUBKEY`).
- **Branch-protection ruleset on `main` requiring signed commits.**
  Currently the ruleset (id 16835594) blocks force-push but does
  not require signatures. This is an admin/web action on the
  GitHub ruleset, not a code change.
- **Automated release-notes generation** from CHANGELOG (or
  `gh release create --generate-notes`) instead of manual
  copy-paste in step 5.2.

When closing each remaining follow-up, update the table in §6 from
"Convention" to "CI" and remove the corresponding line here.

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

*Last reviewed: 2026-05-27 (closes #890; previously closes #837).*
