# Current State & Limitations

> Snapshot as of v0.13.1 (2026-06-10). For the always-current scope, see the
> GitHub [milestones](https://github.com/ToscanAI/orchemist/milestones) and
> [CHANGELOG.md](../CHANGELOG.md).

## What Orchemist is today

A Python engine (`orchemist` on PyPI, v0.13.1) that sequences multi-phase AI
pipelines from YAML templates, plus a web harness (`orch serve`) and a Claude
Code skills pack (separate repo). Alpha-grade (`Development Status :: 3 - Alpha`).
**7795 tests** in the suite; **12** bundled pipeline templates.

## Execution modes

| Mode | What it does | API key | Maturity |
|---|---|---|---|
| `dry-run` | Mock execution; validates structure/interpolation | none | Stable |
| `standalone` | Direct Anthropic Messages API | `ANTHROPIC_API_KEY` | Stable |
| `openrouter` | Any model via OpenRouter (primary production path) | `OPENROUTER_API_KEY` | Stable; 6-tool tool-calling |
| `openclaw` | Sub-agents via the OpenClaw gateway | gateway token | Deprecated (gateway inactive) |

(`claudecode` is an additional executor used only inside a Claude Code MCP session — see below.)

## Shared vs mode-specific

- **Shared across all modes:** the sequencer (phase ordering, dependency
  resolution, output forwarding, retries, the `<MISSING:>` fail-fast guard),
  the 12 YAML templates, template composition (`extends:`/`exclude_phases:`),
  the graders / Scenario Runner, cost tracking, and the SQLite store.
- **Mode-specific:** only the executor that actually calls a model
  (Anthropic / OpenRouter / OpenClaw / ClaudeCode) and its credential.

## Executor maturity

| Executor | Mode | Maturity | Notes |
|---|---|---|---|
| AnthropicExecutor | standalone | **Production** | stdlib `urllib`; primary BYO-key path |
| OpenRouterExecutor | openrouter | **Production** | primary path; 6-tool tool-calling (#794 shipped); local OpenAI-compatible endpoints (Ollama / LM Studio / vLLM) via `--base-url` (#968 shipped) |
| ClaudeCodeExecutor | (MCP session) | **Limited** | uses the user's Claude Code subscription; only inside an MCP tool handler |
| GeminiCliExecutor | (dialogue phase) | **Experimental** | dialogue-phase prototype (#677); no tool-calling/streaming/retry |
| OpenClawExecutor | openclaw | **Deprecated** | gateway no longer active; kept for historical runs |
| DryRunExecutor | dry-run | **Stable** | mock results |

Local OpenAI-compatible endpoints (Ollama / LM Studio / vLLM) ship via
`--base-url` ([#968](https://github.com/ToscanAI/orchemist/issues/968)). Per-phase
provider targeting (the `provider:` phase key — `anthropic` / `openrouter` — for
mixed-provider pipelines) ships via
[#969](https://github.com/ToscanAI/orchemist/issues/969); broadening the provider
matrix further (more families, polish) is tracked in
[#101](https://github.com/ToscanAI/orchemist/issues/101).

## Web harness

Engine-required (#888): with `orch serve` running, the six screens consume real
`/api/v1/*` data; without a reachable engine the `EngineOfflineGuard` shows an
"engine unreachable" error UI (no demo-data fallback). A Playwright e2e suite is
a required CI check on every PR (#889).

## Known limitations

- REST API reference (`docs/rest-api-v1.md`) is hand-maintained and may lag the
  implementation; the live Swagger UI / `web/api.py` is authoritative. Full
  regeneration from the OpenAPI spec is not yet automated.
- Spec adversary occasionally deviates from the first-line verdict format
  (compensated by retry loops at the cost of extra rounds).
- Cost reporting overestimates by ~3x when OpenRouter omits `usage.total_cost` (#801).
- The bash/command tool sandbox is a UX guardrail, not a security boundary —
  use OS-level isolation (firejail, containers) for untrusted workloads.
- `pyproject.toml` project URLs still point at the old `orchestration-engine`
  slug (CODEOWNERS-gated; tracked separately — not fixable in a docs PR).

## Planned

- Provider-matrix expansion — [#101](https://github.com/ToscanAI/orchemist/issues/101).
- Dialogue phase graduating from the `dialogue_phase` flag (default off today) — #677.
- Generic adversary / dialogue runner work — #700, #702, #703.
- Decomposition of the largest modules — #942.

See the GitHub [milestones](https://github.com/ToscanAI/orchemist/milestones)
(v1.0.0 / v1.1 / post-v1) for authoritative scope.
