# Changelog

All notable changes to Orchemist (formerly Orchestration Engine).

## [Unreleased]

### Added
- Directory-level hash guard with `protected_paths` (#706) — `compute_directory_hash()` in `file_guard.py` for recursive, deterministic SHA-256 over directories; pre-execution snapshot + post-execution verification detects unauthorized writes to repo paths like `tests/` or `src/`; excludes `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.git/` by default; symlinks hashed by target path string (not followed); path resolution: `config["repo_path"]` primary, `working_dir` fallback
- Approve-gated file protection with `protect_on_approve` (#718) — adversary/reviewer phases can declare files to seal upon APPROVE verdict; hash snapshot taken at the approve transition (after verdict extraction), stored in `_protected_hashes` for downstream verification; activates on both `approve` and `exhausted` (implicit approval); `coding-pipeline-skip-spec.yaml` updated to guard `acceptance_tests.py` via `acceptance_test_adversary`

## [0.8.0] - 2026-03-28

### Added
- JSON-RPC IPC protocol for orchestrator↔validator communication (#540)
- External validator subprocess with restricted permissions (#539)
- 3-phase spec loop v2.0 — spec → behavioral → adversary (#666) — restructured coding pipeline spec loop with adversarial review
- Incremental spec editing + postmortem phase (#663, #652) — spec edits applied incrementally; postmortem phase for failure analysis
- Git-based phase handoff for spec-loop iterations (#674) — phase outputs handed off via git commits for reliable iteration state
- Iteration-indexed file writing and `{iteration_history}` template variable (#649) — phases write output to iteration-specific files; history of prior iterations available via template variable
- `{iteration_history}` in all looping phase prompts (#650) — ensures all looping phases have full iteration context

### Fixed
- Fix git handoff committing chat summaries instead of agent-written files (#681)
- Fix `GitHandoff.finalize()` overwriting agent-written files with chat summaries (#679)
- Fix missing success fallback on verdict-routed phases causing early termination (#680)
- Fix verdict parser failing on markdown-formatted verdicts (#678)
- Fix `_detect_loop_groups()` including downstream phases in self-loop with success transition (#672)
- Fix `_detect_loop_partners()` only detecting 2-phase loops — 3-phase spec loop had empty iteration_history and stale file reads (#667)
- `MAX_ITERATIONS` error now distinguishes repeated vs new findings (#651) — clearer error messaging at iteration limit
- Spec agent syncs edits to both `spec.md` and `spec-behavioral.md` (#668) — prevents spec drift between files
- `DryRunExecutor` result now includes `text` key; skip required field validation in dry-run mode (#659)
- Updated webhook/trigger test template refs after legacy template removal (#659)
- Adversary phase skips re-raising resolved findings from prior rounds — prevents false regressions
- Success fallback transition added to `spec_adversary` in all coding templates (#645) — prevents stuck runs when adversary approves
- Strengthened spec revision discipline in coding pipeline template (#670) — spec agent follows stricter edit rules

## [0.7.0] - 2026-03-24

### Added
- Claude Code executor (#637) — native `claude --print` executor for local Claude Code pipelines
- `--executor` CLI flag (#636) — override executor at launch/run time without editing templates
- `PipelineRunner.claudecode()` factory method (#638) — convenient constructor for Claude Code executor pipelines
- Adversary exhaustion postmortem — auto-enrich issue on spec failure and re-fetch on retry (#615)
- `coding-pipeline-standard` template (#646) — stable production coding pipeline template
- `coding-pipeline-v2` with codebase preparation phase (#605) — dedicated prepare phase for context loading
- Enriched prepare phase with behavioral contract pre-answers — reduces adversary round-trips
- Content pipeline research agent with web search for independent verification (#570)
- Immutable acceptance test store (#541) — append-only test result store for acceptance run tracking

### Fixed
- Fix CI failure from stale template name assertion after rename (#633)
- Fix CI pattern mismatch in `test_default_output_dir_name_format` (#617)
- Fix 4 independent CI bugs across all Python versions (#628)
- Rewrote f-string backslash expressions for Python 3.10/3.11 compatibility (#618)
- Fix review phase consistently producing empty output (#619)
- Deduplicated `list_templates()` by template id instead of filename stem (#614) — prevents ghost entries for aliased templates
- Renamed coding pipelines to intent-based names (direct commit) — `coding-pipeline-standard`, `coding-pipeline-with-prep`; v1/v2 kept as backward-compat aliases
- CI fixture decoupling (#632) — `coding-pipeline-fixture.yaml` in `examples/`; `ORCH_DEFAULT_TEMPLATE` env-var support; lint enforcement test

### Documentation
- Standalone Mode Guide — Claude Code + Cursor with `ANTHROPIC_API_KEY` (#630)
- Tutorial and monitoring guide (#574) — end-to-end walkthrough and ops reference
- MCP integration guide (#611) — Claude Code + Cursor setup with Orchemist MCP server
- GitHub issue templates for docs/content/research pipeline types (#575)
- Troubleshooting guide with stale output dir lesson
- `CONTEXT_GUIDE.md` for writing effective `files_context` entries
- PyPI publish workflow (#573) — automated release to PyPI on version tag; PyPI badge in README

## [0.6.0] - 2026-03-18

### Added
- `orch launch --issue <number>` CLI shorthand for issue-driven launches (#591)
- `orch validate` + dry-run pass for all bundled templates (#577)
- GitHub Actions CI workflow (#572)
- MCP server scaffold + transport layer (#467) — foundation for Model Context Protocol integration
- MCP core tools: `launch`, `status`, `logs` (#468)
- MCP E2E integration test for full tool chain (#471)
- `docs-pipeline-v1` for technical documentation (#608) — 3-phase doc workflow (research → draft → review)
- Added failed transitions to all pipeline phases (#602) — prevents stuck runs on unexpected output
- Added file-handoff to research + editorial templates (#596)

### Fixed
- Fix test failures from forensics remediation (#571)
- Fix test failures — deleted template references (#587)
- Fix CLI and wizard test failures from forensics remediation (#588)
- Fix test failures in REST API, web serve, and validation suites (#589)
- Fix preflight to use template `config_schema` instead of hardcoded coding fields (#576)
- Removed misleading "possible rate limit" from stall warnings (#581)
- Priority ordering in `extract_verdict()` to prevent APPROVE shadowing REQUEST_CHANGES (#600)
- Fixed `depends_on` and config defaults in `research-competitive-v2` (#594)
- Stripped markdown fences before JSON parse in diagnosis module (#579)
- Enforced max_retries cap in adaptive retry engine (#580)
- Gated postflight routing and GitHub hooks by template category (#578)
- Fixed prepare phase to output text reply, not file write (direct commit)

### Documentation
- Trust hardening risks added to `ROADMAP.md` (#569)
- Forensics audit (8 reports) — docs-vs-implementation gap analysis and remediation plan
- Post-remediation operational advisory for non-technical operators

## [0.5.0] - 2026-03-11

### Added

#### Preflight & Postflight
- Preflight (Definition of Ready) (#476) — pre-run checks: required config, budget, template validity
- Postflight (Definition of Done) (#476) — post-run actions: summary generation, result delivery
- Post run summary to GitHub issue (#487) — postflight posts structured summary to originating issue
- Create gate file for git-enabled pipelines (#495) — guard file written before git operations
- Deferred auto-merge (#499) — PR created before merge attempted; merge deferred until gates pass
- Graceful shutdown / cancel orphaned sessions (#488) — SIGTERM handler cancels in-flight OpenClaw sessions

#### Cost Tracking & Budget
- Per-phase LLM cost tracking (#457) — `cost_tracking` table with model/token/cost per phase
- Budget enforcement (#458) — per-run budget cap checked at preflight and enforced during execution
- Cost API endpoints (#459) — REST endpoints for querying cost data per run/phase
- Record phase cost / enforce per-run budget (#496) — daemon-side budget enforcement

#### Issue Automation & Webhooks
- LLM-based issue classification (#452) — 6 categories (bug/feature/docs/refactor/research/content)
- Template selector + input extractor (#453) — auto-match templates to classified issues
- Issue automation orchestrator (#454) — confidence gate (≥0.70), GitHub comment utility, webhook endpoint
- `post_result_to_issue` (#455) — unified dispatch facade for posting pipeline results back to issues
- GitHub issue fetcher (#507) — fetch issue data via `gh` CLI with structured output
- Pipeline-ready label trigger (#511) — `slugify_branch`, `generate_pipeline_input`, `remove_github_label`
- Sprint chain automation (#514) — post-merge sprint chain with `add_github_label`, `get_github_issue_labels`, state table
- Telegram HITL callback (#446) — callback endpoint with quiet-hours gate for human-in-the-loop notifications
- GitHub App authentication module (#510) — JWT-based auth for GitHub API integrations
- CI event handler: `check_suite` triggers regression detection (#512)
- Sprint runner meta-template (#506) — for multi-issue sprint execution

#### Scoring & Quality Gates (Sprint 7)
- `acceptance_run` phase field (#532) — mark phases as acceptance test runs for scoring
- File-guard hash verification (#531) — SHA-256 hash checks for protected output files
- Acceptance test phase in coding pipeline (#530)
- `acceptance_pass_rate` as primary confidence signal (#528) — 0.40 weight in composite score
- Code quality check pass rate signal (#533) — CI linting/test results feed into confidence
- `spec_adversary` reward recording (#546) — adversarial spec reviewer reward tracked per run
- Behavioral E2E integration tests for full trust chain (#534)
- End-to-end integration test for daemon pipeline lifecycle (#501)

#### Orchestration
- Cascading verdict extraction for reliable auto-merge decisions (#493)
- Review phase enforces APPROVE/REQUEST_CHANGES as first line of output (#486)
- Pipeline situational awareness — phase context preamble (#435)

### Fixed
- Don't treat gateway-retryable API errors as terminal (#482) — 502/503/429 now trigger retry instead of abort
- Fix 63 failing tests from Sprint 4+5 regressions (#503)
- Auto-merge fires before PR is created — race condition fixed (#499)
- Daemon never creates gate file — auto-merge can't find branch (#495)
- Killing pipeline daemon does not cancel active sub-agent sessions (#488)
- Implement phase branch checkout to prevent branch drift during execution (#412)

## [0.4.0] - 2026-03-08

### Added

#### Trust Calibration
- `TrustProfile` / `TrustConfig` dataclasses (#424) — per-pipeline trust state and configuration
- `TrustCalibrator` EMA-based updater (#425) — exponential moving average trust score tracking
- Trust penalty for regressions (#426) — detected regressions reduce trust score; routing context updated
- Idle-profile trust decay (#427) — unused profiles decay toward baseline; trust API endpoints

#### Review Outcomes & Calibration
- Structured review output — template + parser (#385)
- Durable review outcome storage (#386) — `review_outcomes` table persists every review result
- Review catch-value signal for composite confidence (#387) — reviewer accuracy feeds scoring
- Adversarial audit phase (#388) — post-pipeline second-opinion reviewer for high-stakes outputs
- Reviewer calibration (#389) — longitudinal accuracy tracking per reviewer identity
- Structured summary & calibration snapshots (#390) — historical calibration signal for review phases

#### Confidence & Self-Healing
- Confidence threshold calibration (#442) — auto-merge at ≥0.90, human-review at ≤0.70
- Register GitHub webhook + wire regression trigger (#443)
- Activate auto-merge with calibrated confidence thresholds (#444)
- Self-healing regression fix dispatch (#445) — end-to-end self-healing chain integration test
- Confidence escalation + end-to-end integration (#456)

#### CLI & Developer Experience
- `orch watch` (#414) — stream pipeline run events in real-time via CLI
- `orch children` / `orch chain` (#508) — inspect chain hierarchy and monitor child runs
- Validate required config fields (#411) — pre-run check for missing config variables
- Stall detection (#413) — rate-limit event emission for SSE and automatic stall alerts

#### Regression Tracking
- Regressions table (#397) — `regressions` table for regression event tracking
- `RegressionDetector` — breaking commit identification (#398)
- `ci_green_shas` table / webhook wiring + GitHub issue creation (#399)
- `RegressionFixer` — spawn fix pipeline (#400)
- Confidence-gated auto-merge for fixes (#401)
- Safety guards — loop prevention + exclusions (#402)

## [0.3.0] - 2026-03-06

### Added

#### Web UI v2
- Next.js project scaffold (#303) — minimal buildable skeleton
- UI primitives: Button, Badge, and design system components (#304)
- API client library and TypeScript types (#305)
- SSE hook for real-time pipeline progress (#306)
- Dashboard page with template grid (#307)
- Template detail page with phase plan and launch form (#308)
- CLI commands for FastAPI SPA serving (#310)
- `RunStatusBadge` + `PhaseEventRow` UI components (#319)
- Run detail page with live SSE progress view (#320)
- Template CRUD — create, update, delete, validate (#324)

#### Pipeline Chaining
- Pipeline chaining config (#364) — `on_complete` block in templates with placeholder interpolation
- Chain execution in daemon (#365) — child pipelines spawned automatically on parent completion
- Self-referential chain validation (#366) — static cycle detection, chain DAG validation, children REST/CLI API

#### Confidence Scoring & Routing
- Confidence scoring module (#367) — 9 weighted signals producing composite confidence scores
- Confidence-based routing config (#368) — threshold validation and routing rules in templates
- Routing decisions table (#369) — `routing_decisions` table; daemon dispatches based on confidence
- Review queue (#370) — Pydantic models, REST endpoints, and notification hooks for human review

#### Issue Automation & Webhooks
- Webhook trigger configuration (#359) — `triggers` table with generic webhook matching rules
- Webhook endpoint with GitHub signature verification (#360) — rate-limit enforcement and invocation history
- Payload filter matching and input map interpolation (#361)
- Trigger CRUD REST API and rate limiting (#362)
- End-to-end GitHub webhook integration test (#363)

#### Diagnosis & Error Recovery
- Diagnosis results table (#391) — `diagnosis_results` table for failure-diagnosis subsystem
- LLM-powered diagnostician (#392) — Haiku-based failure analysis with structured prompt template
- Systemic failure detection (#393) — error normalization + SHA-256 hashing; 3+ occurrences in 7 days triggers alert
- Adaptive retry strategy engine (#394) — 6 retry strategies with per-failure-class defaults
- Strategy executors — parameter modification (#395)
- Daemon integration for adaptive retry (#396) — cost estimation and model escalation ladder (Haiku→Sonnet→Opus)
- Retry escalation status / cost estimation (#396) — retry cost tracked and capped

#### Templates & Scoring
- Content-based verdict extraction / routing (#301) — verdict keywords parsed from phase output to drive transitions
- Output length validation (#351) — reject phase output exceeding configured max length
- Per-repo auto-merge config (#350) — `auto_merge` settings per repository in templates
- Configurable `allowed_commands` per template (#348)
- Auto-inject `Closes #N` in PR body for issue auto-close (#349)
- Circuit-breaker registry (#346) — per-endpoint breaker with exponential-backoff retry
- Model fallback chain (#347) — automatic escalation through model tiers on repeated failures
- Judge file-reference handoff: pass file paths to scoring judge (#286)
- Post-pipeline auto-scoring (#172) — automatic rubric-based scoring after run completion
- Scoring status/score columns (#287) — `scoring_status` and `score` on `pipeline_runs` table
- Gate final pipeline status on scoring outcome (#288) — runs marked `failed` if score is below threshold
- Score gate enforcement (#289) — error status set on scoring exception; configurable pass threshold

### Fixed
- Fix scoring judge to read output files instead of captured session text (#383)
- Fix LLM judge to read all phase `.md` files, not just `_final_output.md` (#294)
- Fix OpenClawExecutor to delegate `command` task_type to LocalCommandExecutor (#328)
- Fix 33 stale tests from template count drift, schema enum, and phase evolution (#265)
- `sessions_history` pagination limit workaround (#239) — handle API pagination ceiling
- Poll timeout raised from 600s to 1200s (#240) — prevent premature timeout on long-running sessions
- Session cleanup detection (#241) — detect and handle externally cleaned-up OpenClaw sessions

## [0.2.0] - 2026-03-01

### Added

#### REST API & Async Daemon
- FastAPI REST API (#257) — full CRUD for pipeline runs, templates, and configuration
- SSE live-progress streaming (#258) — `pipeline_run_events` table for real-time event delivery
- Async pipeline runs table (#267) — daemon-based background execution with status tracking
- OpenClaw subscription token authentication (#272) — secure daemon-to-gateway token handling
- Template CRUD API with validation (#259)

#### State Machine & Phase Transitions
- Phase transitions data model (#231) — fast phase lookup by ID and automatic default transitions
- Transition graph helpers (#232) — validation, advisory checks, and transition graph utilities
- Outcome determination — map `TaskResult` to `PhaseOutcome` (#233)
- `StateMachineSequencer` — linear transitions (#234)
- Loop / iteration support (#235) — phases can declare loop conditions for iterative execution
- Runner integration + CLI progress for state machine transitions (#236)

#### Output & File Handling
- File-path handoff between pipeline phases (#243) — eliminate inline re-embedding
- Git-based content handoff (#249) — content pipeline uses commit pattern
- Write FILE blocks to disk (#189) — parse and persist `FILE:` blocks from phase output to disk

#### Orchestration
- Content Pipeline v2.4 (#180–#184) — orchestrator improvements, phase output accumulation, wave grouping (#185)
- Non-TTY progress heartbeat (#186) — background progress logging for CI/cron environments (#216)
- Command execution phase type (#190) — `command` executor for shell-command phases
- Retry-feedback tracking (#192) — attempt history persisted per phase for downstream analysis
- Supervisor hook (#194) — prompt template interception and phase-level supervisor overrides
- Coding templates must declare a scenario (#295) — quality-gate enforcement for coding pipelines

### Fixed
- Fix sub-agents not writing output files to disk in file-path handoff (#247)
- Remove `OUTPUT_CAPTURE_INSTRUCTION` after file-path handoff (#245)
- Structured HTTP error classification in OpenClaw executor (#244)
- Detect and handle gateway session cleanup during polling (#241)
- Add poll timeout to prevent infinite hang on cleaned-up sessions (#240)
- Paginate sessions_history to capture full sub-agent output (#239)

## [0.1.0] - 2026-02-27

### Added

#### Phase Execution & Orchestration
- `orch run` E2E wiring — `PipelineRunner` + CLI integration + tests (closes #36)
- Parallel phase execution (#102) — ThreadPoolExecutor for concurrent wave execution; RLock→Lock fix + `fail_fast` documented as best-effort
- Phase retry logic (#209) — `retries` and `retry_delay_seconds` on `PhaseDefinition` (#215)
- Output capture fix (#210) — hybrid prompt instruction + full transcript capture (#211)
- Terminal state detection (#212) — `stopReason=error` and `max_tokens` now correctly halt pipelines (#213)

#### Templates
- Three example pipeline templates for quickstart (#77)
- `orch new` scaffold command (#73) — generate a new template skeleton interactively
- Documentation field checks (#78) — template linting now validates `description` and `docs` fields
- Git integration for coding pipelines (#147) — auto-commit/push hooks in code-development phases (#179)
- `context_files` (#148) — inline local files directly into sub-agent prompts
- `security-audit` and `ux-audit` pipeline templates
- `docs-pipeline` template — 3-phase documentation workflow (research → draft → review)
- `orch rubric generate` (#122) — skill-to-rubric parser for LLM judge graders
- Knowledge-work task types (#123) — new schema types for knowledge-intensive phases
- Research-competitive and editorial-rewrite pipeline templates
- Content Pipeline v2.5/v2.6 — heartbeat module + updated bundled templates

#### CLI & Developer Experience
- `orch templates list / info` (#67, #68) — browse and inspect bundled + installed templates (#116)
- `orch templates install / uninstall` (#69) — install community templates from the index (#118)
- `orch quickstart` (#65) and `orch start` interactive wizard (#66) (#117)
- Default output directory + Markdown output + Rich progress bars (#70–#72) (#115)
- Template resolution order (#75) — project-local → user-global → bundled; partial name/ID matching
- Enhanced `orch validate` (#74) — comprehensive YAML linting with actionable error messages
- Community template index (#76) — searchable discovery of third-party templates
- `orch serve` — local web UI (FastAPI + htmx) for running pipelines via browser (#79)

#### Web UI (v1)
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

#### Testing & Scenarios
- 200-test extended QA suite (#110) — covers previously untested internals (#187)
- CI dry-run scenario testing for all bundled templates (#173) (#178)
- Scenario files for all bundled templates (#170) (#177)
- E2E autonomous scenario test (#108)
- Scenario runner MVP — assertion, LLM judge, and URL graders

### Fixed
- Sub-agent output capture — 3 separate bugs fixed (empty output, truncation, encoding) (#136)
- Template variable interpolation edge cases + token counting (#134, #135)
- OpenClaw executor now uses `/tools/invoke` gateway API (#133)
- Per-phase timeout support in OpenClaw executor
- Token count extraction — query `sessions_list` for `totalTokens`
- `depends_on` required for editorial-rewrite phases (phase output resolution)
- Phase IDs must use underscores — hyphens break `str.format` interpolation
- `config[]` syntax for input variables in editorial-rewrite template
- Template field aliases + unknown field warnings (#205, #206)
- Output directory now uses UUID suffix to prevent collisions (#146, #149)
- Template list source column truncation (#176)
- `config_schema` defaults validation (#145) — reject invalid default values in template schemas
- Fix OpenClaw executor silent failure on large prompts (~45KB+) (#208)
- Fix OpenClaw executor passing empty prompt on phase output handoff (#204)
- AST-based eval allowlist + path traversal protection in scenario assertions
- Shell injection and path traversal fixes + thread safety + DB correctness
- Path traversal protection for `skill_refs` (#120)
- API key masked in `ExecutorResult.__repr__` (#119)
- Security warnings (#141–#145) — env var leak prevention, grader key masking, path info exposure, schema defaults
- Security audit (#156–#160) — log redaction for API keys, doc drift cleanup, grader documentation
- XSS sanitization + script tag nesting fix (#82, #84)
- Guard NaN in cost totals + timer leak on double-start (#83)
- HITL `resume_event` timeout + test teardown hang
- UX launch blockers (#151–#155) — phantom doc links, broken cross-references
- Template lookup now supports partial ID/name matching
- `--fix` comment warning and permission error handling (#74)
- `CliRunner mix_stderr` error and `sqlite3` datetime deprecation
- Pydantic V2 compatibility (`Config` → `ConfigDict`)
- Test and DB compatibility shims for `fetch_all` and `ON CONFLICT`
- Pipeline abort on phase failure + `SafeDict` wrapper for config access

### Documentation
- Initial CHANGELOG.md created (#219)
- Fix output directory paths in GETTING_STARTED.md (#218)
- Remove `orchestra-templates.md` describing non-existent Python API (#217)
- Web UI (`orch serve`) usage documentation (#220)
- Gemini/fallback executor configuration documented (#221)
- Fix ARCHITECTURE.md: add AnthropicExecutor, update executor diagram (#222)
- Historical docs marked with version banners (#223)
- Phase retry field documentation in Template Authoring Guide (#224)
- Unified model naming across all documentation (#225)
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
