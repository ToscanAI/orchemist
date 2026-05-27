# Maintainers

This file lists the people authorised to cut releases of `orchemist` and
to approve changes to supply-chain-critical files (see
`.github/CODEOWNERS`).

## Active maintainers

| Handle | Name | Scope | Contact |
|---|---|---|---|
| @Conny-Lazo | René Rivera | Release authority; full repo | conny.lazo@gmail.com |

## What a maintainer does

- Reviews and approves PRs that touch `pyproject.toml`,
  `.github/workflows/publish.yaml`, `docs/RELEASE-SOP.md`,
  `.github/CODEOWNERS`, this file, and `CODE_OF_CONDUCT.md`.
- Tags and publishes releases per `docs/RELEASE-SOP.md`.
- Handles incoming reports under `CODE_OF_CONDUCT.md` (see that file
  for the response timeline).

## Adding or removing a maintainer

Open a PR editing this file. An existing maintainer must approve. The
PR must also update `.github/CODEOWNERS` if the new maintainer should
own any path patterns.

## Reporting issues

For code-of-conduct reports, contact a maintainer at the email above.
For security reports, see `SECURITY.md`.
