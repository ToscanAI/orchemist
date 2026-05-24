<!-- One short summary line below — what changed and why. -->

## Summary

-

## Why now

<!-- The driving signal: an incident, a user request, an issue, a pivot decision. Link the issue. -->

## What this PR is NOT

<!-- Keep diffs focused. List anything an reviewer might expect that is deliberately out of scope so they don't search for it. -->

-

## How verified

<!-- Match the engine's verification posture: full test suite + targeted reproduction, not just the touched file. -->

- [ ] `python3 -m pytest -q` — full suite passes (cite the duration / count)
- [ ] Behavior reproduced manually — <one-line how>
- [ ] No new warnings from `ruff`, `mypy`, or the type-check step
- [ ] Frontend (if touched): `npm run build` clean; pages still render; no console errors

## Risk assessment

<!-- Pick one and justify. -->

- Reversible / low blast-radius / no migration → low risk
- Touches sealed artifacts / migration / API contract → medium risk — explicit rollback plan included below
- Touches shared infra / branch protection / cross-repo → high risk — needs explicit ack from another human

## Rollback plan (if medium / high risk)

<!-- One concrete revert path. "git revert <sha>" is usually fine; if not, say so. -->

## References

- Closes #
- Epic:
- Related:

🤖 If applicable, note which Orchemist phase produced this PR (e.g. coding-pipeline-standard `b90a3719`).
