# Changelog

All notable changes to the Orchestration Engine.

## [Unreleased]

### Added

#### Phase Execution & Orchestration
- Parallel phase execution (#102) — ThreadPoolExecutor for concurrent wave execution; RLock→Lock fix + fail_fast documented as best-effort
- Phase retry logic (#209) — `retries` and `retry_delay_seconds` on `PhaseDefinition` (#215)
- Non-TTY progress heartbeat (#186) — background progress logging for CI/cron environments (#216)
- Output capture fix (#210) — hybrid prompt instruction + full transcript capture (#211)
- Terminal state detection (#212) — `stopReason=error` and `max_tokens` now correctly halt pipelines (#213)
- Content Pipeline v2.4 (#180–#184) — orchestrator improvements, phase output accumulation, wave grouping (#185)
- `orch run` E2E wiring — `PipelineRunner` + CLI integration + tests (closes #36)

#### Templates
- Content Pipeline v2.5/v2.6 — heartbeat module + updated bundled templates
- Research-competitive and editorial-rewrite pipeline templates
- Git integration for coding pipelines (#147) — auto-commit/push hooks in code-development phases (#179)
- `context_files` (#148) — inline local files directly into sub-agent prompts
- `security-audit` and `ux-audit` pipeline templates
- `docs-pipeline` template — 3-phase documentation workflow (research → draft → review)
- `orch rubric generate` (#122) — skill-to-rubric parser for LLM judge graders
- Three example pipeline templates for quickstart (#77)
- `orch new` scaffold command (#73) — generate a new template skeleton interactively

#### CLI & Developer Experience
- `orch templates list / info` (#67, #68) — browse and inspect bundled + installed templates (#116)
- `orch templates install / uninstall` (#69) — install community templates from the index (#118)
- `orch quickstart` (#65) and `orch start` interactive wizard (#66) (#117)
- Default output directory + Markdown output + Rich progress bars (#70–#72) (#115)
- Template resolution order (#75) — project-local → user-global → bundled; partial name/ID matching
- Enhanced `orch validate` (#74) — comprehensive YAML linting with actionable error messages
- Community template index (#76) — searchable discovery of third-party templates
- `orch serve` — local web UI (FastAPI + htmx) for running pipelines via browser (#79)

#### Web UI
- Template selector with card grid, search, and category filter (#80)
- Auto-generated input forms from `config_schema` (#81)
- Live progress via SSE with enriched phase events and real-time status (#83)
- Visual phase display + Markdown output viewer (#82, #84)

#### Executors & Integrations
- Gemini fallback executor with OpenAI-compatible API (#119) — automatic fallback when Anthropic quota is exceeded
- OpenClaw mode wiring with real sub-agent execution (#100)
- `AnthropicExecutor` — direct API executor with no OpenClaw dependency
- `skill_refs` (#120) — inject external prompt files into phase prompts
- Executor routing in `LLMJudgeGrader` (#171) — judge phases can use any configured executor (#175)

#### Testing & Scenarios
- 200-test extended QA suite (#110) — covers previously untested internals (#187)
- CI dry-run scenario testing for all bundled templates (#173) (#178)
- Scenario files for all bundled templates (#170) (#177)
- E2E autonomous scenario test (#108)
- Scenario runner MVP — assertion, LLM judge, and URL graders

### Fixed

#### Orchestration
- Sub-agent output capture — 3 separate bugs fixed (empty output, truncation, encoding) (#136)
- Template variable interpolation edge cases + token counting (#134, #135)
- OpenClaw executor now uses `/tools/invoke` gateway API (#133)
- Per-phase timeout support in OpenClaw executor
- Token count extraction — query `sessions_list` for `totalTokens`
- `depends_on` required for editorial-rewrite phases (phase output resolution)
- Phase IDs must use underscores — hyphens break `str.format` interpolation
- `config[]` syntax for input variables in editorial-rewrite template

#### Templates & Validation
- Template field aliases + unknown field warnings (#205, #206)
- Output directory now uses UUID suffix to prevent collisions (#146, #149)
- Template list source column truncation (#176)
- `test_framework` input variable lookup in test generation prompt

#### Security
- Security audit (#156–#160) — log redaction for API keys, doc drift cleanup, grader documentation
- Security warnings (#141–#145) — env var leak prevention, grader key masking, path info exposure, schema defaults
- AST-based eval allowlist + path traversal protection in scenario assertions
- Shell injection and path traversal fixes + thread safety + DB correctness
- Path traversal protection for `skill_refs` (#120)
- API key masked in `ExecutorResult.__repr__` (#119)

#### Web UI
- XSS sanitization + script tag nesting fix (#82, #84)
- Guard NaN in cost totals + timer leak on double-start (#83)
- HITL `resume_event` timeout + test teardown hang

#### CLI & UX
- UX launch blockers (#151–#155) — phantom doc links, broken cross-references
- Template lookup now supports partial ID/name matching
- `--fix` comment warning and permission error handling (#74)
- `CliRunner mix_stderr` error and `sqlite3` datetime deprecation
- Pydantic V2 compatibility (`Config` → `ConfigDict`)
- Test and DB compatibility shims for `fetch_all` and `ON CONFLICT`
- Pipeline abort on phase failure + `SafeDict` wrapper for config access

### Documentation
- `CONTRIBUTING.md` + Template Authoring Guide (#112, #113) — YAML reference, field docs, cookbook patterns
- nohup instructions for long-running OpenClaw pipelines (#150)
- Timeout best practices added to Template Authoring Guide
- Hero launch README rewrite (#111)
- Architecture review for output extraction (#136)
- Tech stack, API reference, and scenario strategy documents
