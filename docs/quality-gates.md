# Quality Gates

Quality in the orchestration engine is enforced through the **Scenario Runner** — a standalone evaluation system that loads YAML scenario files and grades pipeline output against acceptance criteria.

> **Note:** The original in-engine gate system (`QualityGate`, `GateResult`, etc.) described in earlier drafts of this document was not implemented. Quality enforcement is done via the Scenario Runner instead.

## How Quality Works

1. Each scenario defines **acceptance criteria** — a mix of hard gates and scored checks.
2. The `ScenarioRunner` grades a pipeline's output against those criteria.
3. Gates (weight 0) must all pass or the scenario fails immediately.
4. Scored criteria produce a weighted average; the scenario passes if it exceeds `pass_threshold`.

## The Three Graders

### 1. Assertion Grader (`graders/assertion.py`)

Evaluates a Python boolean expression against the pipeline output dict.

**Security model:** Restricted `eval` — only `len`, `str`, `int`, `float`, `bool` builtins available. Explicit blocklist of dangerous patterns (`__import__`, `exec(`, `eval(`, `open(`, etc.).

```yaml
- id: word_count_in_range
  type: assertion
  check: "800 <= len(output.get('article', '').split()) <= 1500"
  weight: 0   # weight: 0 → hard gate (must pass)
```

Returns: `score = 1.0` (truthy) or `0.0` (falsy/error), `grader_type = "assertion"`.

### 2. LLM Judge Grader (`graders/llm_judge.py`)

Calls a language model to score the pipeline output against a rubric.

**Holdout enforced** — the judge model sees the pipeline output and rubric, but not the original brief or instructions. This prevents the judge from scoring effort rather than output quality.

```yaml
- id: factual_accuracy
  type: llm_judge
  judge_model: claude-sonnet-4-6
  rubric_file: shared/rubrics/factual-accuracy.md
  threshold: 0.80
  weight: 3
```

- `rubric_file`: path relative to `scenarios/` root (e.g., `shared/rubrics/factual-accuracy.md`)
- `rubric`: inline rubric text (alternative to `rubric_file`)
- Returns: `score` (0.0–1.0), `grader_type = "llm_judge"`

### 3. URL Check Grader (`graders/url_check.py`)

Extracts URLs from the pipeline output and checks that they return HTTP 200.

```yaml
- id: citation_reachability
  type: url_check
  check: "all URLs in Sources section return HTTP 200"
  threshold: 0.90
  weight: 1
```

Returns: `score` = fraction of URLs that resolved successfully, `grader_type = "url_check"`.

## Scenario YAML Format

```yaml
id: content-pipeline-happy-path-001
version: 1
pipeline: content-pipeline

acceptance:
  # Hard gate — binary, must pass (weight: 0)
  - id: article_not_empty
    type: assertion
    check: "len(output.get('article', '')) > 100"
    weight: 0

  # Scored criterion — contributes to weighted average
  - id: factual_accuracy
    type: llm_judge
    judge_model: claude-sonnet-4-6
    rubric_file: shared/rubrics/factual-accuracy.md
    threshold: 0.80
    weight: 3

scoring:
  method: weighted_average
  pass_threshold: 0.75
  gate_mode: all_or_nothing   # any gate failure → scenario fails
```

**Required keys:** `id`, `acceptance` (list).

## Scoring Logic

```
1. Run all criteria → collect CriterionResult list

2. Gate check:
   gates = [cr for cr in results if cr.weight == 0]
   gates_passed = all(cr.grade.passed for cr in gates)

3. Weighted score (non-gate criteria):
   weighted_score = sum(cr.grade.score * cr.weight for cr in scored) / total_weight

4. Pass decision:
   if gate_mode == "all_or_nothing" and not gates_passed:
       scenario_passed = False
   else:
       scenario_passed = weighted_score >= pass_threshold
```

## Current Scenarios

Located in `scenarios/content-pipeline/`:

| File | ID | What it tests |
|------|----|---------------|
| `happy-path-001.yaml` | `content-pipeline-happy-path-001` | Normal article generation with accurate sourcing |
| `hallucination-trap-002.yaml` | `content-pipeline-hallucination-trap-002` | Detects fabricated statistics or citations |
| `ambiguous-brief-003.yaml` | `content-pipeline-ambiguous-brief-003` | Pipeline handles vague/conflicting input gracefully |

## Shared Rubrics

Located in `scenarios/shared/rubrics/`:

| File | Purpose |
|------|---------|
| `factual-accuracy.md` | Are claims supported by cited sources? |
| `no-fabrication.md` | Are statistics and citations real? |
| `structural-quality.md` | Is the article well-structured with headers, flow? |
| `tone-check.md` | Does the tone match the brief? |

## Programmatic Usage

```python
from pathlib import Path
from scenario_runner import ScenarioRunner

runner = ScenarioRunner(scenarios_dir=Path("scenarios/content-pipeline"))

# Load and run one scenario
scenario = runner.load_scenario(Path("scenarios/content-pipeline/happy-path-001.yaml"))
result = runner.run_scenario(scenario, pipeline_output={"article": "..."})

print(result.passed)          # True/False
print(result.weighted_score)  # 0.0–1.0
print(result.gates_passed)    # True/False

# Run all scenarios in a directory
suite_result = runner.run_suite(
    suite_dir=Path("scenarios/content-pipeline"),
    pipeline_outputs={"content-pipeline-happy-path-001": {"article": "..."}, ...}
)
print(suite_result.satisfaction_rate)  # fraction of scenarios that passed
```

See `scenario_runner/README.md` for grader details and `scenarios/README.md` for adding new scenarios.
