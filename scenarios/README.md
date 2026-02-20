# Scenarios

This directory contains YAML scenario files used by the `ScenarioRunner` to evaluate pipeline output.

## Directory Structure

```
scenarios/
├── README.md                          ← you are here
├── content-pipeline/                  ← scenarios for the content pipeline
│   ├── happy-path-001.yaml
│   ├── hallucination-trap-002.yaml
│   └── ambiguous-brief-003.yaml
└── shared/
    └── rubrics/                       ← rubric text files, reusable across scenarios
        ├── factual-accuracy.md
        ├── no-fabrication.md
        ├── structural-quality.md
        └── tone-check.md
```

## Scenario YAML Format

A scenario file defines **acceptance criteria** for a specific pipeline run.

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique scenario identifier |
| `acceptance` | list | List of criteria (see below) |

### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `version` | int | Schema version |
| `pipeline` | string | Pipeline name this tests |
| `name` | string | Human-readable name |
| `tags` | list | Searchable tags |
| `input` | dict | Reference input (docs only — not used in grading) |
| `observations` | list | Metrics to collect (future use) |
| `scoring` | dict | Scoring config (see below) |

### Acceptance Criteria

Each criterion has:

```yaml
acceptance:
  - id: unique_criterion_id         # required
    type: assertion | llm_judge | url_check  # required
    weight: 0                       # 0 = hard gate, >0 = scored
    threshold: 0.75                 # minimum score to pass (default: 0.5)
    # type-specific fields (see below)
```

#### Type: `assertion`

Evaluates a Python boolean expression.

```yaml
- id: word_count_in_range
  type: assertion
  check: "800 <= len(output.get('article', '').split()) <= 1500"
  weight: 0   # hard gate
```

**Safe builtins available:** `len`, `str`, `int`, `float`, `bool`  
**Input variable:** `output` — the pipeline output dict  
**Security:** Blocked patterns: `__import__`, `exec(`, `eval(`, `open(`, etc.

#### Type: `llm_judge`

Calls an LLM to score against a rubric.

```yaml
- id: factual_accuracy
  type: llm_judge
  judge_model: claude-sonnet-4-6
  rubric_file: shared/rubrics/factual-accuracy.md   # relative to scenarios/
  # OR inline:
  # rubric: "Score 0–1 based on whether claims are accurate..."
  threshold: 0.80
  weight: 3
```

**Holdout enforced:** The judge sees only the pipeline output + rubric — not the brief or input. This prevents the judge from rewarding effort over quality.

**Available models:** Any Claude model identifier (e.g., `claude-haiku-4-5-20241022`, `claude-sonnet-4-6`)

#### Type: `url_check`

Checks that URLs in the pipeline output are reachable (HTTP 200).

```yaml
- id: citation_reachability
  type: url_check
  check: "all URLs in Sources section return HTTP 200"  # description only
  threshold: 0.90   # 90% of URLs must resolve
  weight: 1
```

Score = fraction of URLs that returned HTTP 200.

### Scoring Config

```yaml
scoring:
  method: weighted_average    # only supported method
  pass_threshold: 0.75        # weighted avg must exceed this to pass
  gate_mode: all_or_nothing   # any gate failure → scenario fails
```

### Full Example

```yaml
id: content-pipeline-happy-path-001
version: 1
pipeline: content-pipeline
name: "AI testing article with accurate sourcing"
tags: [happy-path, sourcing, linkedin]

input:
  brief: "Write a LinkedIn article about outcome-based testing"
  word_count_range: [800, 1500]

acceptance:
  # Hard gates (weight: 0)
  - id: article_not_empty
    type: assertion
    check: "len(output.get('article', '')) > 100"
    weight: 0

  - id: word_count_in_range
    type: assertion
    check: "800 <= len(output.get('article', '').split()) <= 1500"
    weight: 0

  # Scored criteria
  - id: factual_accuracy
    type: llm_judge
    judge_model: claude-sonnet-4-6
    rubric_file: shared/rubrics/factual-accuracy.md
    threshold: 0.80
    weight: 3

  - id: citation_reachability
    type: url_check
    threshold: 0.90
    weight: 1

scoring:
  method: weighted_average
  pass_threshold: 0.75
  gate_mode: all_or_nothing
```

## Adding a New Scenario

1. Create a YAML file in the appropriate subdirectory:
   ```bash
   touch scenarios/content-pipeline/edge-case-004.yaml
   ```

2. Define `id` and `acceptance` (required). Add criteria.

3. Test it:
   ```python
   from pathlib import Path
   from scenario_runner import ScenarioRunner
   
   runner = ScenarioRunner(scenarios_dir=Path("scenarios/content-pipeline"))
   scenario = runner.load_scenario(Path("scenarios/content-pipeline/edge-case-004.yaml"))
   result = runner.run_scenario(scenario, pipeline_output={"article": "test"})
   print(result)
   ```

4. Add a test in `tests/test_scenarios.py`.

## Rubric Files

Rubric files are markdown documents that guide the LLM judge. Keep them:
- **Specific** — clear criteria, not vague instructions
- **Scoreable** — describe what a 0.0 vs 1.0 score looks like
- **Short** — 1–2 paragraphs is plenty

Rubrics live in `shared/rubrics/` and are referenced by `rubric_file: shared/rubrics/name.md` in scenario criteria.

## Naming Conventions

| Thing | Convention | Example |
|-------|-----------|---------|
| Scenario ID | `{pipeline}-{slug}-{NNN}` | `content-pipeline-happy-path-001` |
| Scenario file | `{slug}-{NNN}.yaml` | `happy-path-001.yaml` |
| Criterion ID | `snake_case` | `factual_accuracy` |
| Rubric file | `kebab-case.md` | `factual-accuracy.md` |
