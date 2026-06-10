# Orchemist Roadmap

> **Last updated:** 2026-06-10
> This file used to hold a long pre-pivot engineering plan. It has been retired
> in favor of GitHub milestones (the live source of truth) and this short pointer.

## Direction: a two-product strategy

- **orchemist-skills** ([ToscanAI/orchemist-skills](https://github.com/ToscanAI/orchemist-skills)) —
  the Orchemist coding pipeline repackaged as Claude Code skills/agents. The
  recommended on-ramp if you already use Claude Code (no Python runtime).
- **The engine** (this repo) — the full orchestration harness. Its differentiator
  is multi-model / multi-provider freedom via a simple model selector: choosing
  models outside Anthropic is the reason the dedicated harness exists.

## Milestones (live on GitHub)

The authoritative, up-to-date scope and progress live in GitHub milestones:

- **v1.0.0**
- **v1.1**
- **post-v1**

See [all milestones](https://github.com/ToscanAI/orchemist/milestones) for the
current scope and progress.

## Release-gate status (#892)

Gates 1, 3, and 4 are complete. The remaining gates are tracked by
[#886](https://github.com/ToscanAI/orchemist/issues/886) and
[#891](https://github.com/ToscanAI/orchemist/issues/891) (external-pilot readiness).

## What's shipping / planned

For what works today, executor maturity tiers, and known limitations, see
[docs/CURRENT-STATE.md](docs/CURRENT-STATE.md). For shipped changes, see
[CHANGELOG.md](CHANGELOG.md). Provider-matrix expansion is tracked in
[#101](https://github.com/ToscanAI/orchemist/issues/101).
