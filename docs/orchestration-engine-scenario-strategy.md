> ⚠️ **HISTORICAL DOCUMENT** — Written February 2026. Many issues described here have since been addressed. See current documentation for up-to-date information.

# Orchestration Engine — Scenario-Based Testing Strategy

**Author:** Independent strategy review  
**Date:** 2026-02-20  
**Status:** Recommendation — ready for implementation  
**Inputs reviewed:** Scenario testing research, dark factory gap research, Nate B. Jones analysis, architecture audit v2, content pipeline protocol, sub-agent standards, engine source code (schemas, runner, recovery)

---

## Executive Summary

The orchestration engine has the right vision but is building in the wrong order. It has 3,600 lines of tests that prove the queue works while the engine can't orchestrate a single agent. Adding scenario-based outcome testing doesn't change what needs building — it changes **why** you build it and **how you know it works.**

**The core recommendation:** Write the scenarios first. They become the acceptance criteria for every piece of infrastructure you build. Don't build a real executor and then figure out how to test it — define what "working" looks like, then build until the scenarios pass.

This is not test-driven development in the conventional sense. It's **outcome-driven development**: the scenarios describe what the system must produce (a publishable article, a working code change), and every infrastructure decision serves those outcomes.

**Timeline:** 6 weeks to a working MVP where one content pipeline scenario runs end-to-end and produces a scored result. 12 weeks to a scenario suite that gives real confidence in pipeline quality.

---

## 1. Scenario Architecture

### Format: YAML + Markdown, Not Gherkin

Use **YAML** for machine-evaluated criteria and **Markdown** for the human-readable scenario narrative. Do not use Gherkin.

**Why not Gherkin:** Gherkin requires step definitions — glue code that maps natural language to executable logic. For AI orchestration, the "steps" are entire agent runs, not function calls. Gherkin adds ceremony without value. The Cucumber ecosystem is designed for web apps with predictable UIs, not non-deterministic multi-agent pipelines.

**Why YAML + Markdown:** YAML is trivially parseable, version-controllable, and composable. Markdown gives the human context that YAML can't express. This mirrors StrongDM's NLSpec approach: structured metadata + narrative description in one package.

**Scenario file structure:**

```yaml
# scenarios/content-pipeline/accurate-sourcing-001.yaml
id: content-pipeline-accurate-sourcing-001
version: 1
pipeline: content-pipeline
name: "AI testing article with accurate sourcing"
created: 2026-02-20
tags: [happy-path, sourcing, linkedin]

# What goes INTO the pipeline
input:
  brief: "Write a LinkedIn article about outcome-based testing for AI agent systems"
  target_audience: "CTOs and engineering leads"
  word_count_range: [800, 1500]
  tone: "practitioner sharing experience, not authority lecturing"
  author: "Conny Lazo"

# What MUST be true about the pipeline's output
acceptance:
  # Hard gates — binary pass/fail
  - id: article_not_empty
    type: assertion
    check: "output.article.word_count > 100"
    weight: 0  # Gate, not scored — fails the whole scenario

  - id: sources_section_exists
    type: assertion
    check: "output.article.has_section('Sources & Inspiration')"
    weight: 0

  - id: word_count_in_range
    type: assertion
    check: "output.article.word_count >= 800 and output.article.word_count <= 1500"
    weight: 0

  # Scored criteria — LLM-judged
  - id: factual_accuracy
    type: llm_judge
    judge_model: "claude-sonnet-4-6"
    rubric: |
      Rate the factual accuracy of this article on a scale of 0.0 to 1.0.
      - 1.0: Every claim is verifiable or clearly marked as author experience
      - 0.7: Most claims are verifiable, a few are unsupported but plausible
      - 0.4: Multiple unsupported claims that could mislead readers
      - 0.0: Contains fabricated statistics, fake citations, or demonstrably false claims
      
      Check specifically:
      - Are cited URLs real and do they support the claims made?
      - Are statistics attributed to named sources?
      - Are "author experience" claims clearly distinguishable from researched facts?
    threshold: 0.80
    weight: 3

  - id: structural_quality
    type: llm_judge
    judge_model: "claude-haiku-4-5"
    rubric: |
      Rate the structural quality of this LinkedIn article (0.0 to 1.0).
      - Has compelling headline
      - Has introduction that states the thesis
      - Has 3+ substantive sections with transitions
      - Has conclusion with call-to-action
      - Sections flow logically (not just juxtaposed topics)
    threshold: 0.70
    weight: 2

  - id: tone_appropriateness
    type: llm_judge
    judge_model: "claude-sonnet-4-6"
    rubric: |
      Rate whether this article maintains the tone of a practitioner sharing experience (0.0 to 1.0).
      - 1.0: Consistently writes from "here's what I found" perspective
      - 0.5: Mixed — some sections read as authoritative pronouncements
      - 0.0: Reads like an expert lecturing the audience on their own field
      
      Red flags: "the industry is...", "developers will need to...", predictions about who thrives/fails
    threshold: 0.75
    weight: 2

  - id: citation_reachability
    type: url_check
    check: "all URLs in Sources section return HTTP 200"
    threshold: 0.90  # Allow one broken link
    weight: 1

# What to measure but NOT gate on (observational)
observations:
  - id: cost_tracking
    measure: "total pipeline cost in USD"
  - id: execution_time
    measure: "total pipeline execution time in seconds"
  - id: model_escalations
    measure: "number of model tier escalations during pipeline"

# Scenario-level pass criteria
scoring:
  method: weighted_average  # of scored criteria (not gates)
  pass_threshold: 0.75
  gate_mode: all_or_nothing  # Any gate failure = scenario failure regardless of score
```

### Storage: Separate Directory, Same Repo

Store scenarios in `/home/toscan/orchestration-engine/scenarios/`, but **the engine code must never import or read from this directory at runtime.** The scenario runner is a separate entry point.

**Why not a separate git repo:** Overhead for one developer. The holdout principle doesn't require physical separation — it requires that the executing agents never see the scenario criteria. Since the scenarios define acceptance criteria (not training data), and the agents don't have filesystem access to the engine's repo during execution, same-repo is fine. The executing agents run in OpenClaw sessions with their own workspace — they can't read `scenarios/`.

**Why not external storage (S3, database):** Unnecessary complexity. YAML files in git give you version control, diffs, blame, and PRs for free. When you have 200+ scenarios, revisit this.

**Directory structure:**

```
orchestration-engine/
├── src/orchestration_engine/    # Engine code (never reads scenarios/)
├── scenarios/
│   ├── shared/
│   │   ├── invariants.yaml      # Universal quality gates
│   │   ├── rubrics/             # Reusable LLM judge rubrics
│   │   │   ├── factual-accuracy.md
│   │   │   ├── structural-quality.md
│   │   │   └── tone-check.md
│   │   └── README.md
│   ├── content-pipeline/
│   │   ├── accurate-sourcing-001.yaml
│   │   ├── ambiguous-brief-002.yaml
│   │   ├── hallucination-trap-003.yaml
│   │   └── failure-recovery-004.yaml
│   ├── code-pipeline/
│   │   └── (future)
│   └── research-pipeline/
│       └── (future)
├── scenario_runner/             # Separate package — runs scenarios against engine
│   ├── __init__.py
│   ├── runner.py                # Loads scenarios, runs pipeline, grades results
│   ├── graders/
│   │   ├── assertion.py         # Binary pass/fail checks
│   │   ├── llm_judge.py         # LLM-as-judge scoring
│   │   └── url_check.py         # HTTP reachability checks
│   ├── report.py                # Generates scenario run reports
│   └── cli.py                   # `python -m scenario_runner run --suite content-pipeline`
├── scenario_results/            # Score history (gitignored, or tracked for regression)
│   └── YYYY-MM-DD-HHMMSS.json
└── tests/                       # Existing unit tests (keep these)
```

### Holdout Separation

The holdout principle means: **the agents executing the pipeline must not see the acceptance criteria while they work.** This is naturally satisfied by the architecture:

1. The orchestration engine spawns sub-agents via `sessions_spawn()`
2. Each sub-agent gets a prompt (from the pipeline template) and a workspace
3. The sub-agent's prompt comes from the template engine, not from the scenario file
4. The scenario's acceptance criteria are only evaluated AFTER the pipeline completes

**The only contamination risk:** If someone copies scenario acceptance criteria into the pipeline template prompts. Don't do this. The pipeline template says "write an article about X" — the scenario says "the article must score >0.8 on factual accuracy." These are different documents with different audiences.

**Enforcement:** A lint check in CI that greps pipeline templates for scenario IDs or rubric text. If a template contains text from a scenario file, fail the build.

### Criterion Types

| Type | When to Use | Implementation |
|------|------------|----------------|
| **Assertion** (binary) | Hard requirements, safety gates | Python expression evaluated against structured output |
| **LLM Judge** (0.0–1.0) | Quality dimensions, subjective criteria | Separate LLM call with rubric prompt, parsed score |
| **URL Check** (fraction) | Citation reachability | HTTP HEAD requests, fraction of 200 responses |
| **Structural Check** (binary) | Schema/format compliance | Pydantic validation of output against expected schema |
| **Diff Check** (binary) | Regression prevention | Compare output sections against known-good baselines |

### Scoring: Weighted Average with Hard Gates

Not binary. Not pure confidence. **Weighted average of scored criteria, gated by binary requirements.**

How it works:
1. Evaluate all gate criteria first. If any gate fails → scenario fails (score 0.0).
2. Evaluate all scored criteria. Each returns 0.0–1.0.
3. Compute weighted average: `sum(score_i × weight_i) / sum(weight_i)`
4. If weighted average ≥ `pass_threshold` → scenario passes.

**Satisfaction rate** across the suite: `scenarios_passed / scenarios_run`. This is the primary KPI. Track it over time. Regressions are visible.

**Why not pure binary:** "Is this article good?" has gradations. An article with 0.82 factual accuracy and 0.71 structure is meaningfully different from one with 0.95/0.90. The weights encode business priorities — factual accuracy matters more than structure, so it gets weight 3 vs weight 2.

**Why not pure confidence:** Ungated confidence hides critical failures. An article could score 0.85 overall while having zero sources. Gates prevent this.

---

## 2. Content Pipeline as First Scenario

### Why Content Pipeline First

Three reasons:
1. **It's the most exercised workflow.** The content pipeline protocol (v2.3) is the most detailed, battle-tested process in the workspace. It has 8 phases with explicit quality gates already defined in English. Converting these to scenarios is straightforward.
2. **It has known failure modes.** Hallucinated names, wrong specs, fabricated model versions, inconsistent numbers across companion posts — the protocol documents specific failures that scenarios can catch.
3. **It's the highest-stakes output.** Published articles with errors cause real reputational damage. This is where outcome validation matters most.

### Phase-Level Acceptance Criteria

Each phase of the content pipeline produces intermediate output. Scenarios should evaluate the **final output**, but phase-level contracts prevent garbage propagation.

| Phase | Output Contract | Phase-Level Check |
|-------|----------------|-------------------|
| 1. Research | `{sources: [{url, title, claim}], summary: str, confidence: float}` | ≥3 sources with reachable URLs |
| 2. Writing | `{article: str, companion_post: str, word_count: int}` | Word count in range, sources section exists |
| 3. Fact-Check | `{corrections: [{claim, issue, source}], confidence: float}` | Confidence score returned, all person names verified |
| 4. Logical Flow | `{issues: [{location, problem, suggestion}], coherence_score: float}` | Coherence score > 0.6 |
| 5. Red Team | `{flags: [{text, severity, reason}], backlash_risk: float}` | Backlash risk assessed (even if low) |
| 6. Consistency | `{mismatches: [{field, article_value, companion_value}]}` | Zero mismatches OR explicit resolution |
| 7. Apply Fixes | `{article: str, companion_post: str, fixes_applied: int}` | All corrections from 3-6 addressed |
| 8. Human Review | `{approved: bool, notes: str}` | (Not automated — this is where scenarios stop) |

**Important:** Phase contracts are NOT scenario acceptance criteria. They're **input validation for the next phase.** The scenario evaluates the pipeline's final output, not its intermediate steps. Phase contracts exist to fail fast — if Phase 1 produces no sources, don't run Phases 2-7 just to discover the article has no citations.

### End-to-End Acceptance Criteria

For the content pipeline, "success" means: **the output article is publishable without human corrections beyond stylistic preference.**

Concrete criteria:

1. **Factual accuracy** (LLM judge, weight 3): All claims verifiable or flagged as experience
2. **Structural quality** (LLM judge, weight 2): Has required sections, logical flow, transitions
3. **Tone** (LLM judge, weight 2): Practitioner sharing experience, not authority lecturing
4. **Citation integrity** (URL check, weight 1): All cited URLs reachable
5. **Consistency** (assertion, gate): Numbers match between article and companion post
6. **Sources section** (assertion, gate): Exists with ≥3 linked references
7. **Word count** (assertion, gate): Within specified range
8. **No fabrication** (LLM judge, weight 3): No invented names, quotes, statistics, or model versions

The weighted score across criteria 1, 2, 3, 4, 8 determines the scenario score. Gates 5, 6, 7 must pass independently.

### Evaluating Article Quality Programmatically

This is the hard problem. "Is this article accurate and well-reviewed?" is a subjective judgment. Here's how to make it tractable:

**Decompose quality into independent, scorable dimensions.** Don't ask one judge "is this article good?" Ask five judges specific questions:

1. **Factual accuracy judge:** "Here is the article. Here is the research brief it was based on. Does the article make any claims that aren't supported by the research brief or verifiable external sources? List each unsupported claim."

2. **Structural judge:** "Here is the article. Does it have: a compelling headline (yes/no), an introduction stating the thesis (yes/no), 3+ substantive sections (count them), transitions between sections (evaluate each), a conclusion (yes/no), a call-to-action (yes/no). Score 0-1."

3. **Tone judge:** "Here is the article. The intended voice is a practitioner sharing what they learned, NOT an expert telling professionals about their field. Rate each paragraph: does it maintain practitioner voice (1.0) or slip into authoritative voice (0.0)? Average across paragraphs."

4. **Fabrication detector:** "Here is the article. List every proper noun (person name, company name, product name, framework name). For each, is it real? List every statistic. Is each attributed to a named source?"

5. **Citation verifier:** (not LLM — HTTP requests) Check each URL in the Sources section.

**Each judge gets a specific rubric, not a vague quality question.** Rubrics are stored in `scenarios/shared/rubrics/` and referenced by scenario files.

### Preventing Judge Bias

The LLM judge must not be biased by the pipeline. Three safeguards:

1. **Different model as judge.** If the pipeline uses Sonnet for writing, use Sonnet for judging (same capability, but a different session with no conversation history). The judge never sees the pipeline's internal reasoning — only the final output.

2. **Rubric-anchored scoring.** The judge doesn't evaluate "quality" in the abstract. It evaluates against a specific rubric with defined score levels. This constrains the judge to the criteria, not its general preference.

3. **Blind evaluation.** The judge receives the article WITHOUT knowing which pipeline produced it, which models were used, or how many phases ran. It just sees: "Here is an article. Here is the rubric. Score it." This prevents the judge from inflating scores because it "knows" the pipeline is sophisticated.

4. **Periodic human calibration.** Every 20 scenario runs, have the human score 3 articles independently. Compare human scores to LLM judge scores. If they diverge by >0.2 consistently, recalibrate the rubric. Track calibration results in `scenario_results/calibration/`.

---

## 3. Integration with Existing Engine

### Where Scenarios Get Evaluated

Scenarios run **outside the engine's main execution path.** The engine runs pipelines. The scenario runner evaluates pipeline outputs. They share a database for results, but the scenario runner is a separate process.

```
┌──────────────────┐     ┌──────────────────────┐
│  Scenario Runner │     │  Orchestration Engine │
│                  │     │                       │
│  1. Load scenario│     │                       │
│  2. Submit input │────>│  1. Receive input     │
│     to engine    │     │  2. Run pipeline      │
│  3. Wait for     │<────│  3. Return output     │
│     completion   │     │                       │
│  4. Grade output │     │                       │
│  5. Record score │     │                       │
└──────────────────┘     └───────────────────────┘
```

**The engine doesn't know it's being tested.** It receives a task (from scenario runner or from a human), runs the pipeline, returns the output. The scenario runner is just another client. This is the holdout principle in action.

### How the Scenario Runner Interacts with the Task Queue

The scenario runner submits tasks through the same API as any other client:

```python
# scenario_runner/runner.py (simplified)

class ScenarioRunner:
    def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        # 1. Create task from scenario input
        task_spec = TaskSpec(
            type=TaskType.CONTENT,
            payload=scenario.input,
            priority=Priority.NORMAL,
            tags=["scenario", scenario.id]
        )
        
        # 2. Submit to engine
        task_id = self.engine.submit(task_spec)
        
        # 3. Wait for completion (poll or callback)
        result = self.engine.wait_for_result(task_id, timeout=scenario.timeout)
        
        # 4. Grade against acceptance criteria
        scores = self.grade(result, scenario.acceptance)
        
        # 5. Compute scenario score
        return ScenarioResult(
            scenario_id=scenario.id,
            task_id=task_id,
            gate_passed=all(s.passed for s in scores if s.is_gate),
            weighted_score=self.compute_weighted_score(scores),
            criterion_scores=scores,
            execution_time=result.execution_time,
            cost=result.cost
        )
```

### Changes to Existing Schemas

**Minimal changes.** The scenario system wraps the engine, it doesn't modify it.

**Add to `TaskResult`:**
```python
# One new optional field
scenario_id: Optional[str] = None  # If this task was submitted by a scenario run
```

**Add to `TaskSpec`:**
```python
# Already has tags — use tags to mark scenario-submitted tasks
# No schema changes needed. Tag convention: tags=["scenario", "scenario-id"]
```

**New schemas (in scenario_runner package, NOT in engine):**

```python
class ScenarioCriterion(BaseModel):
    id: str
    type: Literal["assertion", "llm_judge", "url_check", "structural_check"]
    weight: float = 1.0
    threshold: float = 0.0
    # type-specific fields...

class Scenario(BaseModel):
    id: str
    pipeline: str
    input: Dict[str, Any]
    acceptance: List[ScenarioCriterion]
    scoring: ScoringConfig

class ScenarioResult(BaseModel):
    scenario_id: str
    task_id: str
    gate_passed: bool
    weighted_score: float
    criterion_scores: List[CriterionScore]
    passed: bool  # gate_passed AND weighted_score >= threshold
```

### What Stays the Same

Almost everything. The scenario system is additive:

- **Task queue:** Unchanged. Scenarios submit tasks like any other client.
- **Runner:** Unchanged. It processes tasks regardless of origin.
- **Recovery:** Unchanged. Retries and circuit breakers work the same way.
- **Schemas:** One optional field added to TaskResult.
- **Database:** No changes to existing tables.

The scenario runner, graders, and reporting are a **separate package** (`scenario_runner/`) with its own tables for score history.

### What Needs to Exist First

The scenario runner needs the engine to actually work. Specifically:

1. **Database API must be unified** (the `execute()`/`fetch_all()` mismatch from the audit)
2. **A real executor must exist** (replace `_simulate_openclaw_execution` with actual `sessions_spawn()`)
3. **A minimal template engine** must exist (so the scenario runner can submit "run content pipeline with this input" and the engine knows what phases to execute)
4. **Result capture** must work (the engine must return structured output from the pipeline, not just "task succeeded")

These are the same priorities the audit identified. Scenarios don't change what needs building — they change how you validate it.

---

## 4. Build Plan

### Phase 0: Infrastructure Fixes (Week 1)

This is the audit's Priority 1, unchanged. Scenarios can't run until the engine works.

1. **Fix Database API mismatch.** Add `execute()`, `fetch_all()`, `fetch_one()` to the Database class. Fix inline INDEX syntax. Fix ON CONFLICT issues. Run all 177 tests against the real Database class. (1-2 days)

2. **Implement real `sessions_spawn()` integration.** Replace the simulation stub in `OpenClawExecutor` with actual subprocess calls to OpenClaw. Define how results flow back (recommendation: sub-agent writes to a known file path, engine reads it). (3-4 days)

3. **Verify one task runs end-to-end.** Submit a simple task → engine spawns a real sub-agent → sub-agent produces output → engine captures structured result. No scenarios yet, just proof the engine works. (1 day)

### Phase 1: Scenario Scaffolding (Week 2)

Build the scenario infrastructure in parallel with (or immediately after) the executor.

1. **Create `scenarios/` directory with first 3 scenarios.** (1 day)
   - `content-pipeline/happy-path-001.yaml` — well-defined brief, expects publishable article
   - `content-pipeline/hallucination-trap-002.yaml` — topic requiring specific statistics
   - `content-pipeline/ambiguous-brief-003.yaml` — vague topic, expects clarification or best-effort

2. **Build the scenario runner MVP.** (3 days)
   - Load YAML scenario files
   - Submit task to engine via TaskSpec
   - Wait for completion (polling)
   - Run assertion graders (binary checks on output)
   - Run one LLM judge grader (factual accuracy)
   - Compute weighted score
   - Print results to console

3. **Build the assertion grader.** Evaluates Python expressions against the pipeline's structured output. (included in above)

4. **Build the LLM judge grader.** Calls a judge model with the rubric and the article, parses the 0-1 score from the response. (included in above)

### Phase 2: Minimal Template Engine (Week 3)

The engine needs to know what "content pipeline" means — what phases to run, in what order, with what models.

1. **Define the content pipeline template in YAML.** (1 day)
   ```yaml
   # templates/content-pipeline.yaml
   id: content-pipeline
   name: "Content Pipeline"
   phases:
     - id: research
       model: sonnet-4-6
       prompt_template: "templates/prompts/research.md"
       output_schema: research_output
       timeout: 600
     - id: writing
       model: sonnet-4-6
       depends_on: [research]
       prompt_template: "templates/prompts/writing.md"
       output_schema: article_output
       timeout: 900
     - id: fact_check
       model: sonnet-4-6
       depends_on: [writing]
       prompt_template: "templates/prompts/fact-check.md"
       output_schema: factcheck_output
       timeout: 600
     # ... phases 4-7 (skip 8 — human review is not automated)
   ```

2. **Build the template loader + phase sequencer.** Reads template YAML, creates tasks for each phase in order, passes phase N output as phase N+1 input. No parallel execution in v1. (3 days)

3. **Run first scenario end-to-end.** Scenario runner submits content pipeline input → engine runs 5-phase pipeline (skip red team and consistency check for MVP) → scenario runner grades output. This is the proof-of-concept moment. (2 days of debugging)

### Phase 3: Full Scenario Suite (Week 4-5)

1. **Expand to 10 scenarios for content pipeline.** Cover:
   - Happy path (well-defined brief)
   - Ambiguous brief
   - Hallucination trap (requires specific, verifiable statistics)
   - Length constraint (brief specifies 500 words — must respect)
   - Cross-domain content (writing about translation or law — tone check)
   - Companion post consistency (numbers must match)
   - Source quality (must not cite blogs or Wikipedia as primary)
   - Failure recovery (simulate Phase 3 timeout — graceful degradation)
   - Minimal brief (just a topic, no audience/tone specified)
   - Controversial topic (requires careful hedging)

2. **Build URL check grader.** HTTP HEAD requests against cited URLs. (0.5 day)

3. **Build the reporting system.** JSON results per run + a summary report comparing current run to previous baseline. Show regressions. (1 day)

4. **Run multiple trials per scenario.** Each scenario runs 3 times. Track variance. If satisfaction varies by >0.2 across trials, the pipeline is fragile for that input type. (1 day)

5. **Human calibration round.** Have René score 5 articles independently. Compare to LLM judge scores. Adjust rubric weights if needed. (0.5 day)

### Phase 4: CI Integration + Regression Tracking (Week 6)

1. **Scenario runner as CLI command.** `python -m scenario_runner run --suite content-pipeline --trials 3` (1 day)

2. **Integration with git workflow.** Run scenario suite on feature branches. Compare satisfaction rate to `main`. Block merge if satisfaction drops >5%. (1 day)

3. **Score history tracking.** Append results to `scenario_results/` as timestamped JSON files. Build a simple script that plots satisfaction rate over time. (1 day)

4. **Document the scenario authoring process.** How to write a new scenario, what makes a good rubric, how to calibrate. (0.5 day)

### What to Defer

- **Digital Twin Universe:** Building behavioral clones of external APIs (search, CMS) is valuable but premature. The content pipeline's main external dependency is web search for research. Mock it with cached search results for scenario runs — don't build a full DTU.
- **Automated scenario generation:** Using an LLM to generate new scenarios from the pipeline's failure history. Build this after you have 50+ scenario runs to learn from.
- **Per-phase scoring:** Scoring each phase individually (not just the final output). This adds complexity and is only needed when you have enough data to diagnose which phase is causing failures.
- **Parallel phase execution:** Run phases sequentially for now. Parallelism is an optimization for after the pipeline works.
- **Code pipeline and research pipeline scenarios:** Start with content. Expand to other pipeline types after the scenario infrastructure is proven.
- **Memory system, MCP integration, advanced metrics:** These are the audit's "defer" list. Still defer them.

### Timeline Summary

| Week | Deliverable | Validates |
|------|------------|-----------|
| 1 | DB fix + real executor + one task E2E | Engine can orchestrate one agent |
| 2 | 3 scenarios + scenario runner MVP | Can grade pipeline output |
| 3 | Template engine + first scenario E2E | Full pipeline runs and gets scored |
| 4-5 | 10 scenarios + reporting + calibration | Confidence in pipeline quality |
| 6 | CI integration + regression tracking | Quality regressions are visible |

---

## 5. Anti-Patterns to Avoid

### 1. Scenarios That Test Process, Not Outcomes

**The mistake:** Writing scenarios like "Phase 3 must run after Phase 2" or "The fact-check agent must be a separate Sonnet instance." These test the pipeline's internal process, not its output.

**Why it's tempting:** The content pipeline protocol defines 8 specific phases. It's natural to verify each phase ran. But a scenario that says "Phase 3 must run" passes even if Phase 3 rubber-stamped everything.

**The fix:** Scenarios check output properties, never internal pipeline steps. "The article must have verifiable citations" — not "the fact-check phase must have flagged 0 issues." If the pipeline achieves the outcome by running 3 phases instead of 8, that's fine. The scenario doesn't care how.

**Exception:** Phase contracts (input/output schemas) are not scenarios. They're internal engineering quality — validation within the pipeline to fail fast. Don't confuse them with outcome scenarios.

### 2. Agents Gaming Their Own Scenarios

**The mistake:** The pipeline somehow learns the acceptance criteria and optimizes for them instead of genuine quality.

**How it could happen:**
- Scenario rubric text gets copy-pasted into pipeline template prompts
- An agent reads the `scenarios/` directory during execution
- The scenario acceptance criteria become so specific that passing them is a pattern-matching exercise

**The fix:**
- **Lint check:** CI verifies no scenario text appears in pipeline templates.
- **Workspace isolation:** Sub-agents spawned by the engine get their own workspace, not the engine's repo.
- **Rubric rotation:** Every quarter, rewrite 20% of rubrics with different phrasing but same intent. If scores drop, the pipeline was overfitting to rubric language, not achieving genuine quality.
- **Adversarial scenarios:** Periodically add scenarios designed to catch gaming — e.g., a brief that would produce a great article if the agent ignores the tone requirement, to verify the agent actually checks tone rather than pattern-matching "tone score > 0.75."

### 3. Scenario Proliferation Without Curation

**The mistake:** Writing 100 scenarios that are minor variations of each other. "Article about AI testing" and "Article about AI evaluation" and "Article about AI assessment" — these test the same thing.

**The fix:** Each scenario must test a **distinct failure mode or quality dimension.** Before adding a scenario, answer: "What failure would this catch that no existing scenario catches?" If you can't answer, don't add it.

**Target:** 10-15 scenarios per pipeline type. Enough for confidence, few enough to maintain. If satisfaction rate is 1.0 across all scenarios, add harder ones.

### 4. Treating LLM Judges as Ground Truth

**The mistake:** Trusting the LLM judge's 0.83 score as an objective measurement, then making engineering decisions based on 0.83 vs 0.81.

**Reality:** LLM judges are noisy. The same article might score 0.78 on one run and 0.87 on another. Don't chase small score deltas.

**The fix:**
- Run each scenario 3 times. Use the median score, not the mean.
- Only consider changes significant if they move the satisfaction rate by >5 percentage points.
- Calibrate against human judgment quarterly.
- Never use a single judge call for a go/no-go decision — always use the aggregate across multiple criteria.

### 5. Building the Grading System Before the Pipeline Works

**The mistake:** Spending 3 weeks perfecting the LLM judge rubrics, the weighted scoring system, and the regression dashboard — then discovering the engine can't actually run a pipeline.

**The fix:** Phase 0 (infrastructure fixes) must complete before any scenario work. The scenario runner is useless without a working engine. Build the minimum viable grading (one assertion + one LLM judge) alongside the executor, not before it.

### 6. Conflating Scenario Failure with Pipeline Bug

**The mistake:** A scenario scores 0.65 on "factual accuracy." Is the pipeline broken? Or is the scenario's rubric unreasonable? Or was the input brief genuinely hard?

**The fix:** When a scenario fails, diagnose in this order:
1. **Check the rubric.** Is the judge's reasoning sensible? Read its explanation.
2. **Check the input.** Is the brief well-formed? Garbage in, garbage out isn't a pipeline bug.
3. **Check the output.** Read the article yourself. Do you agree with the judge?
4. **Only then** investigate the pipeline. Which phase produced the issue?

This diagnostic order prevents rubric-chasing — endlessly tweaking rubrics to get better scores without improving actual quality.

---

## 6. How This Changes the Strategy

### The Previous Audit's Sequence

The audit recommended: **Fix DB → Real executor → Phase execution → Context sharing.**

This sequence is correct for building infrastructure. It's wrong for validating outcomes.

### The New Sequence

**Write scenarios → Fix DB → Real executor + assertion grader → Template engine + LLM judge → Phase execution → Context sharing.**

The change: **Scenarios move to the front.** Not because they're built first (the infrastructure must exist to run them), but because they're **defined** first. Writing scenarios before building the executor forces you to answer: "What does this executor need to produce for me to call it working?"

This is the difference between:
- *"Build a real executor, then figure out how to test it"* (the old sequence)
- *"Define what a working executor produces, then build until it produces that"* (the new sequence)

### Concretely, What Changes

1. **Week 1 stays the same.** Fix DB + real executor. You can't run scenarios without a working engine.

2. **Week 2 changes.** Instead of "build more infrastructure," you build the scenario runner and write your first scenarios. This gives you immediate feedback on whether the executor's output is usable.

3. **The template engine is now driven by scenarios.** Instead of designing the template format in the abstract, you design it to produce output that scenarios can grade. This prevents building a template engine that outputs unstructured text that no grader can parse.

4. **Quality gates from the content pipeline protocol become scenario criteria.** The protocol already defines what "done right" looks like for each phase. Convert these to scenario acceptance criteria instead of implementing them as in-engine quality gates. This means the engine stays simpler (no built-in quality gate logic) and the validation is external (where it belongs).

5. **"Context sharing" (the last audit priority) is validated by scenarios.** You know context sharing works when multi-phase scenarios pass — the fact-check phase correctly references the research phase's output, which means context was shared. You don't need a separate context-sharing integration test.

### Should Scenarios Come First?

**Yes, but not in the way you'd expect.**

"Scenarios first" doesn't mean "build the scenario runner before the executor." It means:

1. **Write the scenario YAML files** before writing any new engine code. These are plain text — they don't require a working runner. They just define what success looks like.

2. **Use the scenarios as acceptance criteria** for every piece of infrastructure you build. "The executor is done when scenario happy-path-001 produces an article with >100 words." Not "the executor is done when it calls `sessions_spawn()` without crashing."

3. **Build the scenario runner** alongside the executor, not after it. Week 1: executor. Week 2: scenario runner. Week 3: template engine + first scenario passing. The scenario runner and the engine grow together.

This is test-driven development adapted for AI orchestration: the "tests" are outcome scenarios, the "code" is the entire pipeline, and "passing" means the pipeline produces quality output — not that it executes without errors.

### The Strategic Shift

The audit's most important line was: *"Stop building infrastructure. Start orchestrating agents."*

Scenarios sharpen this: **Start orchestrating agents for a defined purpose, and measure whether the purpose was achieved.**

Without scenarios, you can build a working orchestrator and still not know if it's useful. With scenarios, you know the moment the orchestrator starts producing value — it's the moment the first scenario passes.

---

## Appendix A: First Three Scenarios (Ready to Implement)

### Scenario 1: Happy Path — Well-Defined Brief

```yaml
id: content-pipeline-happy-path-001
version: 1
pipeline: content-pipeline
name: "Well-defined brief produces publishable article"
tags: [happy-path, baseline]

input:
  brief: |
    Write a LinkedIn article about how AI agent orchestration 
    differs from simple LLM API calls. Cover: task decomposition, 
    model tier selection, quality gates, and cost management.
  target_audience: "Engineering managers evaluating AI tooling"
  word_count_range: [800, 1500]
  tone: "practitioner sharing experience"
  author: "Conny Lazo"

acceptance:
  - id: not_empty
    type: assertion
    check: "len(output.get('article', '')) > 100"
    weight: 0

  - id: sources_exist
    type: assertion
    check: "'Sources' in output.get('article', '') or 'sources' in output.get('article', '').lower()"
    weight: 0

  - id: word_count
    type: assertion
    check: "800 <= len(output.get('article', '').split()) <= 1500"
    weight: 0

  - id: factual_accuracy
    type: llm_judge
    judge_model: claude-sonnet-4-6
    rubric_file: shared/rubrics/factual-accuracy.md
    threshold: 0.80
    weight: 3

  - id: structure
    type: llm_judge
    judge_model: claude-haiku-4-5
    rubric_file: shared/rubrics/structural-quality.md
    threshold: 0.70
    weight: 2

  - id: tone
    type: llm_judge
    judge_model: claude-sonnet-4-6
    rubric_file: shared/rubrics/tone-check.md
    threshold: 0.75
    weight: 2

scoring:
  method: weighted_average
  pass_threshold: 0.75
  gate_mode: all_or_nothing
```

### Scenario 2: Hallucination Trap

```yaml
id: content-pipeline-hallucination-trap-002
version: 1
pipeline: content-pipeline
name: "Topic requiring specific statistics — catches hallucinated numbers"
tags: [hallucination, sourcing, adversarial]

input:
  brief: |
    Write a LinkedIn article about the METR study findings on 
    AI-assisted development productivity. Include specific statistics 
    from the study and discuss implications for engineering teams.
  target_audience: "Engineering leaders and CTOs"
  word_count_range: [800, 1500]
  tone: "practitioner sharing experience"
  author: "Conny Lazo"

acceptance:
  - id: not_empty
    type: assertion
    check: "len(output.get('article', '')) > 100"
    weight: 0

  - id: correct_metr_stat
    type: llm_judge
    judge_model: claude-sonnet-4-6
    rubric: |
      The METR study found developers were 19% SLOWER with AI tools 
      (not faster). Developers BELIEVED they were 24% faster.
      
      Score this article:
      - 1.0: Correctly states the 19% slower finding and the perception gap
      - 0.5: Gets the direction right (slower) but wrong percentage
      - 0.0: Claims AI made developers faster, or invents different statistics
      
      Also check: Does it attribute the study to METR? Does it mention 
      it was an RCT with 16 developers? Deduct points for fabricated details.
    threshold: 0.85
    weight: 4

  - id: no_fabricated_stats
    type: llm_judge
    judge_model: claude-sonnet-4-6
    rubric: |
      List every statistic in this article. For each one, determine:
      - Is it attributed to a named source? 
      - Is the source a real, verifiable entity?
      - Could this statistic be verified by visiting the cited source?
      
      Score: (verifiable stats) / (total stats). 
      If the article contains a statistic without any attribution, score 0.5 max.
      If a statistic contradicts its cited source, score 0.0.
    threshold: 0.80
    weight: 3

  - id: sources_linked
    type: assertion
    check: "'metr.org' in output.get('article', '').lower() or 'arxiv' in output.get('article', '').lower()"
    weight: 0

scoring:
  method: weighted_average
  pass_threshold: 0.80
  gate_mode: all_or_nothing
```

### Scenario 3: Ambiguous Brief

```yaml
id: content-pipeline-ambiguous-brief-003
version: 1
pipeline: content-pipeline
name: "Vague brief — should produce reasonable article without hallucinating details"
tags: [edge-case, robustness]

input:
  brief: "Write something about AI and coding"
  # Deliberately minimal — no audience, no word count, no tone specified

acceptance:
  - id: not_empty
    type: assertion
    check: "len(output.get('article', '')) > 100"
    weight: 0

  - id: reasonable_length
    type: assertion
    check: "300 <= len(output.get('article', '').split()) <= 2000"
    weight: 0

  - id: coherent_despite_vague_input
    type: llm_judge
    judge_model: claude-sonnet-4-6
    rubric: |
      This article was produced from a deliberately vague brief: 
      "Write something about AI and coding."
      
      Rate how well the article handles the ambiguity (0.0 to 1.0):
      - 1.0: Picks a clear angle, executes it well, doesn't overreach
      - 0.7: Reasonable article but tries to cover too much ground
      - 0.4: Rambling, no clear thesis, reads like filler
      - 0.0: Hallucinated a specific brief that wasn't given, or 
              produced nonsensical output
    threshold: 0.60
    weight: 2

  - id: no_fabrication
    type: llm_judge
    judge_model: claude-sonnet-4-6
    rubric_file: shared/rubrics/no-fabrication.md
    threshold: 0.80
    weight: 3

  - id: has_some_structure
    type: llm_judge
    judge_model: claude-haiku-4-5
    rubric: |
      Does this article have basic structure? (0.0 to 1.0)
      - Has a title/headline
      - Has some form of introduction
      - Has at least 2 substantive sections
      - Has some form of conclusion
      Score based on how many of these are present and coherent.
    threshold: 0.50
    weight: 1

scoring:
  method: weighted_average
  pass_threshold: 0.65  # Lower threshold — vague input means lower expectations
  gate_mode: all_or_nothing
```

---

## Appendix B: Shared Rubric — Factual Accuracy

```markdown
<!-- scenarios/shared/rubrics/factual-accuracy.md -->
# Factual Accuracy Rubric

You are evaluating the factual accuracy of a LinkedIn article.

## Scoring Scale (0.0 to 1.0)

- **1.0 — Excellent:** Every factual claim is either:
  (a) Attributed to a specific, named source with a URL, OR
  (b) Clearly framed as the author's personal experience ("I found...", "In my experience...")
  No claims appear fabricated or misleading.

- **0.8 — Good:** Most claims are well-sourced. 1-2 claims lack specific attribution 
  but are common knowledge or easily verifiable. No outright fabrication.

- **0.6 — Acceptable:** Several claims lack sources. Some statistics are presented 
  without attribution. However, no claims appear to be fabricated — they're 
  plausible even if unsourced.

- **0.4 — Poor:** Multiple unsourced claims. At least one claim that appears 
  to be fabricated or significantly embellished (a statistic that doesn't match 
  its cited source, a quote that seems invented, a person who doesn't exist).

- **0.2 — Very Poor:** Significant fabrication. Made-up statistics, fictional 
  experts cited by name, URLs that don't exist, or claims that directly 
  contradict their cited sources.

- **0.0 — Unacceptable:** Primarily fabricated content. The article presents 
  fiction as fact.

## Specific Checks

1. **Person names:** Are all named individuals real people? Can you verify they 
   exist and are associated with the claims made?
2. **Statistics:** Are all numbers attributed? Do they match their cited sources?
3. **Product/tool names:** Are they real? Are version numbers accurate?
4. **Quotes:** Are quotes attributed? Do they seem authentic?
5. **URLs (if visible):** Do cited sources appear to be real websites?

## Output Format

Score: [0.0-1.0]
Reasoning: [2-3 sentences explaining the score]
Issues found: [list each factual concern, or "None"]
```

---

## Appendix C: Satisfaction Rate Tracking Format

```json
{
  "run_id": "2026-02-25-143022",
  "timestamp": "2026-02-25T14:30:22Z",
  "suite": "content-pipeline",
  "engine_version": "0.3.0",
  "git_sha": "abc123",
  "trials_per_scenario": 3,
  "results": [
    {
      "scenario_id": "content-pipeline-happy-path-001",
      "trials": [
        {"score": 0.82, "gates_passed": true, "passed": true},
        {"score": 0.78, "gates_passed": true, "passed": true},
        {"score": 0.85, "gates_passed": true, "passed": true}
      ],
      "median_score": 0.82,
      "passed": true,
      "variance": 0.0012
    },
    {
      "scenario_id": "content-pipeline-hallucination-trap-002",
      "trials": [
        {"score": 0.71, "gates_passed": true, "passed": false},
        {"score": 0.88, "gates_passed": true, "passed": true},
        {"score": 0.76, "gates_passed": true, "passed": true}
      ],
      "median_score": 0.76,
      "passed": true,
      "variance": 0.0074
    }
  ],
  "satisfaction_rate": 0.90,
  "scenarios_passed": 9,
  "scenarios_run": 10,
  "total_cost_usd": 2.47,
  "total_execution_time_seconds": 1834,
  "comparison_to_baseline": {
    "baseline_run": "2026-02-20-091500",
    "baseline_satisfaction": 0.80,
    "delta": 0.10,
    "regressions": [],
    "improvements": ["hallucination-trap-002"]
  }
}
```

---

*Strategy document completed 2026-02-20. Reviewer has no ongoing relationship with the project.*
