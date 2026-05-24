---
name: Duplicate-function cleanup
about: Consolidate two or more implementations of the same behavior identified by a Phase 0 inventory or duplicate audit
labels: enhancement
---

<!-- Use this template for any "delete the parallel implementation and re-export from the canonical one" work. -->
<!-- The pipeline agent reads ONLY this issue body — keep behavioral context self-contained. -->

## What is duplicated

- **Symbol(s):** <fully-qualified name(s) — e.g. `verdict_parser.extract_verdict` / `review_parser.extract_verdict`>
- **Verdict:** <CONSUME | EXTEND | DIVERGENT | NEW-OK — per pipeline-YAML v4.2 verdict labels>
- **Existing canonical:** <file:line where the surviving implementation lives, or where it should live>

## Evidence

- [Phase 0 inventory artifact, if available]
- [DUPLICATES.md group, if filed]
- [#issue numbers where this duplication was previously observed]

## Behavioral Contracts

### Byte-identical behavior

- Given <call site>, the behavior SHALL be byte-identical before and after the refactor.
- Given <test fixture or sealed manifest>, recorded hashes / outputs SHALL not change.

### Edge cases

- Given <input shape the canonical handles but the duplicate didn't>, the behavior SHALL match the canonical.
- Given <input shape the duplicate handled but the canonical didn't>, EITHER the canonical SHALL be extended to handle it (verdict = EXTEND) OR a `## Divergence justification` SHALL state why the contract drops it.

## Files affected

- `src/orchestration_engine/<canonical>.py` — usually no signature change
- `src/orchestration_engine/<duplicate>.py` — delete the function; re-export the canonical name if any caller imports it
- Call sites: <list>
- Tests: <list>

## Acceptance Criteria

- [ ] `grep -r "<duplicate symbol>" src/` returns zero matches (or only re-exports)
- [ ] Full suite passes: `python3 -m pytest -q`
- [ ] No sealed-manifest hash changes for unchanged inputs
- [ ] Removed lines counted in PR body (so the consolidation win is visible)
