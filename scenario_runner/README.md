# Scenario Runner

The Scenario Runner is a **standalone quality evaluation system** that loads YAML scenario files and grades pipeline output against acceptance criteria using three grader types.

## Directory Structure

```
scenario_runner/
├── README.md              ← you are here
├── __init__.py            ← exports ScenarioRunner
├── runner.py              ← ScenarioRunner, run_scenario(), run_suite()
├── models.py              ← GradeResult, CriterionResult, ScenarioResult, SuiteResult
└── graders/
    ├── __init__.py
    ├── assertion.py       ← restricted eval grader
    ├── llm_judge.py       ← LLM-based rubric scoring
    └── url_check.py       ← HTTP reachability grader
```

## Quick Start

```python
from pathlib import Path
from scenario_runner import ScenarioRunner

runner = ScenarioRunner(scenarios_dir=Path("scenarios/content-pipeline"))

# Load scenario
scenario = runner.load_scenario(Path("scenarios/content-pipeline/happy-path-001.yaml"))

# Grade pipeline output
result = runner.run_scenario(
    scenario,
    pipeline_output={"article": "Full article text..."}
)

print(f"Passed: {result.passed}")             # True/False
print(f"Score: {result.weighted_score:.2f}")  # 0.0–1.0
print(f"Gates: {result.gates_passed}")        # True/False

for cr in result.criterion_results:
    print(f"  {cr.criterion_id}: {cr.grade.score:.2f} ({'PASS' if cr.grade.passed else 'FAIL'})")
```

## The Three Graders

### 1. `AssertionGrader` — Restricted eval

Evaluates a Python boolean expression against the pipeline output dict.

```python
from scenario_runner.graders.assertion import AssertionGrader

grader = AssertionGrader()
result = grader.grade(
    check_expression="len(output.get('article', '')) > 100",
    output={"article": "Some article text"}
)
# result.score = 1.0 (truthy) or 0.0 (falsy/error)
# result.grader_type = "assertion"
```

**Security model:**
- `eval()` runs in a restricted namespace
- Only `len`, `str`, `int`, `float`, `bool` are available as builtins
- Explicit blocklist of dangerous patterns (pre-scanned before eval):
  - `__import__`, `__builtins__`, `__class__`, `__subclasses__`
  - `exec(`, `eval(`, `open(`, `compile(`, `getattr(`, `setattr(`
  - `globals(`, `locals(`, `vars(`, `dir(`
- `output` is the only variable in scope

**Return values:** `score = 1.0` if truthy, `0.0` if falsy, `0.0` if eval error (error message in `details`)

### 2. `LLMJudgeGrader` — Rubric-based scoring

Calls a language model to score the pipeline output against a rubric.

```python
from scenario_runner.graders.llm_judge import LLMJudgeGrader

grader = LLMJudgeGrader()
result = grader.grade(
    pipeline_output={"article": "..."},
    rubric_text="Score 0–1 based on whether claims are supported...",
    judge_model="claude-haiku-4-5-20241022"
)
# result.score = float 0.0–1.0
# result.grader_type = "llm_judge"
```

**Holdout policy (enforced):**
- The judge receives the pipeline **output** and the **rubric**
- The judge does NOT receive the original brief, input, or instructions
- This prevents the judge from scoring effort vs. output quality

**Scoring contract:** The rubric must instruct the model to return a score between 0.0 and 1.0. The grader parses the model's response to extract this score.

### 3. `URLCheckGrader` — HTTP reachability

Extracts URLs from the pipeline output text and checks each one with an HTTP HEAD request.

```python
from scenario_runner.graders.url_check import URLCheckGrader

grader = URLCheckGrader()
result = grader.grade(article_text="...text with https://example.com links...")
# result.score = fraction of URLs returning HTTP 200
# result.grader_type = "url_check"
```

- Extracts `http://` and `https://` URLs from the article text
- Score = `successful_requests / total_urls`
- If no URLs found: `score = 1.0` (vacuously true)
- Tolerates connection errors (counts as failure, doesn't raise)

## Data Models (`models.py`)

```python
class GradeResult:
    passed: bool
    score: float          # 0.0–1.0
    details: str          # human-readable explanation
    grader_type: str      # "assertion" | "llm_judge" | "url_check"

class CriterionResult:
    criterion_id: str
    grade: GradeResult
    weight: int           # 0 = gate, >0 = scored
    is_gate: bool         # True when weight == 0

class ScenarioResult:
    scenario_id: str
    passed: bool
    weighted_score: float         # weighted avg of non-gate criteria
    gates_passed: bool
    criterion_results: list[CriterionResult]
    observations: dict            # declared but not yet populated

class SuiteResult:
    scenarios: list[ScenarioResult]
    satisfaction_rate: float      # fraction of scenarios that passed
    total_scenarios: int
```

## Scoring Algorithm

```python
# 1. Grade all criteria
for criterion in scenario["acceptance"]:
    raw = grade_criterion(criterion, pipeline_output)
    passed = raw.score >= criterion["threshold"]

# 2. Gate check
gates = [cr for cr in results if cr.weight == 0]
gates_passed = all(cr.grade.passed for cr in gates)

# 3. Weighted score (non-gate criteria only)
scored = [cr for cr in results if cr.weight > 0]
total_weight = sum(cr.weight for cr in scored)
weighted_score = sum(cr.grade.score * cr.weight for cr in scored) / total_weight

# 4. Pass decision
if gate_mode == "all_or_nothing" and not gates_passed:
    scenario_passed = False
else:
    scenario_passed = weighted_score >= pass_threshold
```

## Running a Suite

```python
suite = runner.run_suite(
    suite_dir=Path("scenarios/content-pipeline"),
    pipeline_outputs={
        "content-pipeline-happy-path-001": {"article": "..."},
        "content-pipeline-hallucination-trap-002": {"article": "..."},
    }
)

print(f"{suite.satisfaction_rate:.0%} scenarios passed ({suite.total_scenarios} total)")
for r in suite.scenarios:
    icon = "✅" if r.passed else "❌"
    print(f"  {icon} {r.scenario_id}: {r.weighted_score:.2f}")
```

If a scenario ID is not in `pipeline_outputs`, an empty dict `{}` is used — all assertions will fail.

## Adding a New Grader

1. Create `scenario_runner/graders/my_grader.py`
2. Implement a class with a `grade(...)` method that returns a `GradeResult`
3. Register it in `ScenarioRunner._grade_criterion()` by adding a new `elif criterion_type == "my_type":` branch
4. Add tests in `tests/test_scenario_runner.py`

## CLI

> ⚠️ **Not yet implemented.** A `orch scenario run` CLI command is planned for Week 6.

For now, use the Python API directly.
