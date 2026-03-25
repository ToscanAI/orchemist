# Changelog

All notable changes to Orchemist (formerly Orchestration Engine).

## [Unreleased]

### Added

#### Phase Execution & Orchestration
- Parallel phase execution (#102) — ThreadPoolExecutor for concurrent wave execution; RLock→Lock fix + `fail_fast` documented as best-effort
- Phase retry logic (#209) — `retries` and `retry_delay_seconds` on `PhaseDefinition` (#215)
- Retry-feedback tracking (#192) — attempt history persisted per phase for downstream analysis
- Supervisor hook (#194) — prompt template interception and phase-level supervisor overrides
- Non-TTY progress heartbeat (#186) — background progress logging for CI/cron environments (#216)
- Output capture fix (#210) — hybrid prompt instruction + full transcript capture (#211)
- Terminal state detection (#212) — `stopReason=error` and `max_tokens` now correctly halt pipelines (#213)
- Content Pipeline v2.4 (#180–#184) — orchestrator improvements, phase output accumulation, wave grouping (#185)
- Phase transition defaults (#231) — fast phase lookup by ID and automatic default transitions
- Transition graph helpers (#232) — validation, advisory checks, and transition graph utilities
- Loop / iteration support (#235) — phases can declare loop conditions for iterative execution
- Content-based verdict extraction / routing (#301) — verdict keywords parsed from phase output to drive transitions
- `orch run` E2E wiring — `PipelineRunner` + CLI integration + tests (closes #36)
- Iteration-indexed file writing and `{iteration_history}` template variable (#649, PR #653) — phases write output to iteration-specific files; history of prior iterations available via template variable
- `{iteration_history}` in all looping phase prompts (#650, PR #655) — ensures all looping phases have full iteration context
- 3-phase spec loop v2.0 — spec → behavioral → adversary (#666) — restructured coding pipeline spec loop with adversarial review
- Incremental spec editing + postmortem phase (#663, #652, PR #665) — spec edits applied incrementally; postmortem phase for failure analysis
- Git-based phase handoff for spec-loop iterations (#674, PR #675) — phase outputs handed off via git commits for reliable iteration state

#### Templates
- Content Pipeline v2.5/v2.6 — heartbeat module + updated bundled templates
- Research-competitive and editorial-rewrite pipeline templates
- Git integration for coding pipelines (#147) — auto-commit/push hooks in code-development phases (#179)
- `context_files` (#148) — inline local files directly into sub-agent prompts
- `security-audit` and `ux-audit` pipeline templates
- `docs-pipeline` template — 3-phase documentation workflow (research → draft → review)
- `orch rubric generate` (#122) — skill-to-rubric parser for LLM judge graders
- Knowledge-work task types (#123) — new schema types for knowledge-intensive phases
- Three example pipeline templates for quickstart (#77)
- `orch new` scaffold command (#73) — generate a new template skeleton interactively
- Documentation field checks (#78) — template linting now validates `description` and `docs` fields
- Command execution phase type (#190) — `command` executor for shell-command phases
- Output length validation (#351) — reject phase output exceeding configured max length
- Per-repo auto-merge config (#350) — `auto_merge` settings per repository in templates
- `acceptance_run` phase field (#532) — mark phases as acceptance test runs for scoring
- File-guard hash verification (#531) — SHA-256 hash checks for protected output files
- Coding templates must declare a scenario (#295) — quality-gate enforcement for coding pipelines

#### CLI & Developer Experience
- `orch templates list / info` (#67, #68) — browse and inspect bundled + installed templates (#116)
- `orch templates install / uninstall` (#69) — install community templates from the index (#118)
- `orch quickstart` (#65) and `orch start` interactive wizard (#66) (#117)
- Default output directory + Markdown output + Rich progress bars (#70–#72) (#115)
- Template resolution order (#75) — project-local → user-global → bundled; partial name/ID matching
- Enhanced `orch validate` (#74) — comprehensive YAML linting with actionable error messages
- Community template index (#76) — searchable discovery of third-party templates
- `orch serve` — local web UI (FastAPI + htmx) for running pipelines via browser (#79)
- `orch watch` (#414) — stream pipeline run events in real-time via CLI
- `orch children` / `orch chain` (#330.3, #508) — inspect chain hierarchy and monitor child runs
- Validate required config fields (#411) — pre-run check for missing config variables

#### Web UI
- Template selector with card grid, search, and category filter (#80)
- Auto-generated input forms from `config_schema` (#81)
- Live progress via SSE with enriched phase events and real-time status (#83)
- Visual phase display + Markdown output viewer (#82, #84)
- Output preview in `phase_complete` SSE event (#85) — richer real-time feedback
- Human-in-the-loop pause/resume (#86) — pipeline runs can be paused and resumed from the UI

#### Executors & Integrations
- Gemini fallback executor with OpenAI-compatible API (#119) — automatic fallback when Anthropic quota is exceeded
- OpenClaw mode wiring with real sub-agent execution (#100)
- `AnthropicExecutor` — direct API executor with no OpenClaw dependency
- `skill_refs` (#120) — inject external prompt files into phase prompts
- Executor routing in `LLMJudgeGrader` (#171) — judge phases can use any configured executor (#175)
- Circuit-breaker registry (#346) — per-endpoint breaker with exponential-backoff retry
- Model fallback chain (#347) — automatic escalation through model tiers on repeated failures
- GitHub App authentication module (#510) — JWT-based auth for GitHub API integrations

#### Testing & Scenarios
- 200-test extended QA suite (#110) — covers previously untested internals (#187)
- CI dry-run scenario testing for all bundled templates (#173) (#178)
- Scenario files for all bundled templates (#170) (#177)
- E2E autonomous scenario test (#108)
- Scenario runner MVP — assertion, LLM judge, and URL graders

#### REST API & Async Daemon
- FastAPI REST API (#257) — full CRUD for pipeline runs, templates, and configuration
- SSE live-progress streaming (#258) — `pipeline_run_events` table for real-time event delivery
- Async pipeline runs table (#267) — daemon-based background execution with status tracking
- OpenClaw subscription token authentication (#272) — secure daemon-to-gateway token handling
- Stall detection (#413) — rate-limit event emission for SSE and automatic stall alerts

#### Scoring & Quality Gates
- Post-pipeline auto-scoring (#172) — automatic rubric-based scoring after run completion
- Scoring status/score columns (#287) — `scoring_status` and `score` on `pipeline_runs` table
- Gate final pipeline status on scoring outcome (#288) — runs marked `failed` if score is below threshold
- Score gate enforcement (#289) — error status set on scoring exception; configurable pass threshold
- `acceptance_pass_rate` as primary confidence signal (#528) — 0.40 weight in composite score
- Code quality check pass rate signal (#533) — CI linting/test results feed into confidence
- `spec_adversary` reward recording (#546) — adversarial spec reviewer reward tracked per run

#### Pipeline Chaining
- Pipeline chaining config (#330.1) — `on_complete` block in templates with placeholder interpolation
- Chain execution in daemon (#330.2) — child pipelines spawned automatically on parent completion
- Self-referential chain validation (#330.3) — static cycle detection, chain DAG validation, children REST/CLI API
- Chain monitoring CLI (#508) — `parent_run_id` index for fast chain traversal

#### Confidence Scoring & Routing
- Confidence scoring module (#331.1) — 9 weighted signals producing composite confidence scores
- Confidence-based routing config (#331.2) — threshold validation and routing rules in templates
- Routing decisions table (#331.3) — `routing_decisions` table; daemon dispatches based on confidence
- Review queue (#331.4) — Pydantic models, REST endpoints, and notification hooks for human review
- Confidence threshold calibration (#429.1) — auto-merge at ≥0.90, human-review at ≤0.70
- Review catch-value signal (#387) — reviewer finding rate feeds composite confidence
- Self-healing regression fix dispatch (#429.4) — daemon triggers automated fix when regression detected

#### Trust Calibration
- `TrustProfile` / `TrustConfig` dataclasses (#4.2.1) — per-pipeline trust state and configuration
- `TrustCalibrator` EMA-based updater (#4.2.2) — exponential moving average trust score tracking
- Trust penalty for regressions (#4.2.3) — detected regressions reduce trust score; routing context updated
- Idle-profile trust decay (#4.2.4) — unused profiles decay toward baseline; trust API endpoints

#### Review Outcomes & Calibration
- Durable review outcome storage (#4.1.2) — `review_outcomes` table persists every review result
- Review catch-value signal for composite confidence (#4.1.3) — reviewer accuracy feeds scoring
- Adversarial audit phase (#4.1.4) — post-pipeline second-opinion reviewer for high-stakes outputs
- Reviewer calibration (#4.1.5) — longitudinal accuracy tracking per reviewer identity
- Structured summary & calibration snapshots (#4.1.6) — historical calibration signal for review phases

#### Diagnosis & Error Recovery
- Diagnosis results table (#3.1.1) — `diagnosis_results` table for failure-diagnosis subsystem
- LLM-powered diagnostician (#3.1.2) — Haiku-based failure analysis with structured prompt template
- Systemic failure detection (#3.1.3) — error normalization + SHA-256 hashing; 3+ occurrences in 7 days triggers alert
- Adaptive retry strategy engine (#3.2.1) — 6 retry strategies with per-failure-class defaults
- Daemon integration for adaptive retry (#3.2.3) — cost estimation and model escalation ladder (Haiku→Sonnet→Opus)
- Retry escalation status / cost estimation (#396) — retry cost tracked and capped

#### Regression Tracking
- Regressions table (#3.3a.1) — `regressions` table for regression event tracking
- `ci_green_shas` table (#3.3a.3) — last-known-green CI SHA tracking per repository

#### Issue Automation & Webhooks
- Webhook trigger configuration (#329.1) — `triggers` table with generic webhook matching rules
- Webhook invocation log (#329.2) — rate-limit enforcement and invocation history
- LLM-based issue classification (#5.1.1) — 6 categories (bug/feature/docs/refactor/research/content)
- Template selector + input extractor (#5.1.2) — auto-match templates to classified issues
- Issue automation orchestrator (#5.1.3) — confidence gate (≥0.70), GitHub comment utility, webhook endpoint
- `post_result_to_issue` (#5.1.4) — unified dispatch facade for posting pipeline results back to issues
- GitHub issue fetcher (#507) — fetch issue data via `gh` CLI with structured output
- Pipeline-ready label trigger (#511) — `slugify_branch`, `generate_pipeline_input`, `remove_github_label`
- Sprint chain automation (#514) — post-merge sprint chain with `add_github_label`, `get_github_issue_labels`, state table
- Telegram HITL callback (#429.5) — callback endpoint with quiet-hours gate for human-in-the-loop notifications

#### Cost Tracking & Budget
- Per-phase LLM cost tracking (#5.2.1) — `cost_tracking` table with model/token/cost per phase
- Budget enforcement (#5.2.2) — per-run budget cap checked at preflight and enforced during execution
- Cost API endpoints (#5.2.3) — REST endpoints for querying cost data per run/phase
- Record phase cost / enforce per-run budget (#496) — daemon-side budget enforcement

#### Preflight & Postflight
- Preflight (Definition of Ready) (#476) — pre-run checks: required config, budget, template validity
- Postflight (Definition of Done) (#476) — post-run actions: summary generation, result delivery
- Post run summary to GitHub issue (#487) — postflight posts structured summary to originating issue
- Create gate file for git-enabled pipelines (#495) — guard file written before git operations
- Deferred auto-merge (#499) — PR created before merge attempted; merge deferred until gates pass
- Graceful shutdown / cancel orphaned sessions (#488) — SIGTERM handler cancels in-flight OpenClaw sessions

#### Output & File Handling
- Write FILE blocks to disk (#189) — parse and persist `FILE:` blocks from phase output to disk

#### Executors & Integrations (continued)
- Claude Code executor (#637, PR #644) — native `claude --print` executor for local Claude Code pipelines
- `--executor` CLI flag (#636, PR #643) — override executor at launch/run time without editing templates
- MCP server scaffold + transport layer (#467, PR #598) — foundation for Model Context Protocol integration
- `PipelineRunner.claudecode()` factory method (#638) — convenient constructor for Claude Code executor pipelines
- Immutable acceptance test store (#541) — append-only test result store for acceptance run tracking

#### Templates (continued)
- `coding-pipeline-v2` with codebase preparation phase (#605, PR #606) — dedicated prepare phase for context loading
- Renamed coding pipelines to intent-based names (PR #5168199) — `coding-pipeline-standard`, `coding-pipeline-with-prep`; v1/v2 kept as backward-compat aliases
- `coding-pipeline-standard` template (#646) — stable production coding pipeline template
- `docs-pipeline-v1` for technical documentation (#608, PR #609) — 3-phase doc workflow (research → draft → review)
- `hello-pipeline` for E2E testing and demos — minimal pipeline for smoke tests and quickstart
- Enriched prepare phase with behavioral contract pre-answers — reduces adversary round-trips
- Adversary phase skips re-raising resolved findings from prior rounds — prevents false regressions
- Success fallback transition added to `spec_adversary` in all coding templates (#645) — prevents stuck runs when adversary approves
- Strengthened spec revision discipline in coding pipeline template (#670) — spec agent follows stricter edit rules

#### Testing & Scenarios (continued)
- MCP E2E integration test for full tool chain (#471, PR #612) — end-to-end test covering MCP server lifecycle
- Behavioral E2E integration tests for full trust chain (#534, PR #547)
- CI fixture decoupling (#632, PR #634) — `coding-pipeline-fixture.yaml` in `examples/`; `ORCH_DEFAULT_TEMPLATE` env-var support; lint enforcement test

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
- `config_schema` defaults validation (#145) — reject invalid default values in template schemas

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

#### Executors & Reliability
- Don't treat gateway-retryable API errors as terminal (#482) — 502/503/429 now trigger retry instead of abort
- `sessions_history` pagination limit workaround (#239) — handle API pagination ceiling
- Poll timeout raised from 600s to 1200s (#240) — prevent premature timeout on long-running sessions
- Session cleanup detection (#241) — detect and handle externally cleaned-up OpenClaw sessions

#### Templates & Validation (continued)
- Deduplicated `list_templates()` by template id instead of filename stem (#614, PR #627) — prevents ghost entries for aliased templates
- Fixed 4 independent CI bugs across all Python versions (#628, PR #629)
- Rewrote f-string backslash expressions for Python 3.10/3.11 compatibility (#618, PR #626)
- Removed misleading "possible rate limit" from stall warnings (#581, PR #621)
- Priority ordering in `extract_verdict()` to prevent APPROVE shadowing REQUEST_CHANGES (#600, PR #604)
- Added failed transitions to all pipeline phases (#602, PR #603) — prevents stuck runs on unexpected output
- Added file-handoff to research + editorial templates (#596, PR #597)
- Fixed `depends_on` and config defaults in `research-competitive-v2` (#594, PR #595)
- Stripped markdown fences before JSON parse in diagnosis module (#579, PR #582)
- Enforced max_retries cap in adaptive retry engine (#580, PR #583)
- Gated postflight routing and GitHub hooks by template category (#578, PR #586)
- Fixed prepare phase to output text reply, not file write (PR #320976f)
- `MAX_ITERATIONS` error now distinguishes repeated vs new findings (#651, PR #656) — clearer error messaging at iteration limit
- Spec agent syncs edits to both `spec.md` and `spec-behavioral.md` (#668) — prevents spec drift between files
- `DryRunExecutor` result now includes `text` key; skip required field validation in dry-run mode (#659)
- Updated webhook/trigger test template refs after legacy template removal (#659)

### Documentation
- `CONTRIBUTING.md` + Template Authoring Guide (#112, #113) — YAML reference, field docs, cookbook patterns
- nohup instructions for long-running OpenClaw pipelines (#150)
- Timeout best practices added to Template Authoring Guide
- Hero launch README rewrite (#111)
- Architecture review for output extraction (#136)
- Tech stack, API reference, and scenario strategy documents
- REST API v1 reference — 33 endpoints across 8 groups
- Database schema reference — 21 tables, 20 migrations, 22+ indexes
- Confidence scoring & trust calibration guides
- Pipeline chaining documentation — `on_complete`, chain DAG, daemon integration
- Diagnosis & error recovery guide — failure classes, adaptive retry, model escalation
- Issue automation documentation — classification, webhooks, sprint chain
- Forensics audit (8 reports) — docs-vs-implementation gap analysis and remediation plan
- Post-remediation operational advisory for non-technical operators
- Standalone Mode Guide — Claude Code + Cursor with `ANTHROPIC_API_KEY` (#631)
- Tutorial and monitoring guide (#574, PR #622) — end-to-end walkthrough and ops reference
- MCP integration guide (#611, PR #620) — Claude Code + Cursor setup with Orchemist MCP server
- GitHub issue templates for docs/content/research pipeline types (#575, PR #610)
- Troubleshooting guide with stale output dir lesson
- `CONTEXT_GUIDE.md` for writing effective `files_context` entries
- Trust hardening risks added to `ROADMAP.md` (#569)
- PyPI publish workflow (#573, PR #623) — automated release to PyPI on version tag; PyPI badge in README
- Preflight `git_clean` troubleshooting entry added to troubleshooting guide
- Git documented as runtime dependency for pipeline execution
- GitHub auto-link warning added to issue templates
