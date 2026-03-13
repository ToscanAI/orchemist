# Confidence Scoring & Routing

> **Source modules:**
> [`confidence.py`](../src/orchestration_engine/confidence.py) · 
> [`routing.py`](../src/orchestration_engine/routing.py) · 
> [`scoring.py`](../src/orchestration_engine/scoring.py) · 
> [`review_catch_value.py`](../src/orchestration_engine/review_catch_value.py) · 
> [`reviewer_calibration.py`](../src/orchestration_engine/reviewer_calibration.py) · 
> [`audit.py`](../src/orchestration_engine/audit.py)

Every completed pipeline run produces a **composite confidence score** in [0.0, 1.0]. The score aggregates up to 9 weighted signals extracted from phase output files and review records. A routing engine then maps the score to one of four action tiers — auto-merge, human review, retry, or reject.

---

## Table of Contents

1. [Pipeline Overview](#1-pipeline-overview)
2. [Signals](#2-signals)
3. [Weight Tables](#3-weight-tables)
4. [Composite Score Calculation](#4-composite-score-calculation)
5. [Confidence Levels](#5-confidence-levels)
6. [Routing Engine](#6-routing-engine)
7. [Trust-Profile Dynamic Routing](#7-trust-profile-dynamic-routing)
8. [Dynamic Weight Calibration](#8-dynamic-weight-calibration)
9. [Review Catch Value](#9-review-catch-value)
10. [Adversarial Audit](#10-adversarial-audit)
11. [Reviewer Calibration](#11-reviewer-calibration)
12. [Scenario-Based Scoring](#12-scenario-based-scoring)
13. [Template Configuration](#13-template-configuration)

---

## 1. Pipeline Overview

```
Pipeline completes
       │
       ▼
┌──────────────┐     ┌─────────────────┐     ┌─────────────────┐
│ Scenario     │────→│ Confidence      │────→│ Routing         │
│ Scoring      │     │ Calculator      │     │ Engine          │
│ (scoring.py) │     │ (confidence.py) │     │ (routing.py)    │
└──────────────┘     └─────────────────┘     └────────┬────────┘
                                                      │
                            ┌─────────────────────────┼─────────────────────────┐
                            │                         │                         │
                            ▼                         ▼                         ▼
                    ┌──────────────┐          ┌──────────────┐          ┌──────────────┐
                    │ Auto-merge   │          │ Human Review │          │ Retry / Reject│
                    │ (≥ 0.90)     │          │ (≥ 0.70)     │          │ (< 0.70)      │
                    └──────────────┘          └──────────────┘          └──────────────┘
```

**Sequence:**
1. `run_scoring()` runs the template's scenario file (grader criteria) against phase outputs.
2. `ConfidenceCalculator.compute_confidence()` reads task-result JSON files from the output directory and produces up to 9 signals.
3. Signals are combined into a weighted average (renormalised over present signals).
4. `RoutingEngine.evaluate()` maps the composite score to a routing tier, optionally consulting a trust profile for dynamic thresholds.
5. The daemon records the `RoutingDecision` in the `routing_decisions` DB table and transitions the run's status accordingly.

---

## 2. Signals

Each signal is a normalised value in [0.0, 1.0] with an associated weight. Absent signals are simply omitted — the weighted average renormalises over whatever signals are present.

| # | Signal | Source | Description |
|---|---|---|---|
| 1 | `acceptance_pass_rate` | `acceptance_results.json` | Pass rate of spec-derived behavioral acceptance tests. **Primary signal.** Reads `pass_rate` or computes `passed / total`. Skips placeholder records (`status: "tests_written"`, `total: 0`). |
| 2 | `code_quality` | `code_quality_results.json` | Pass rate from automated code quality checks (linting, type-checking). Same file format as acceptance results. |
| 3 | `llm_judge` | Review/judge task JSON files | Average `confidence` field across tasks classified as review or judge (`task_type == "review"` or `"judge"`, or filename contains `"review"`). |
| 4 | `test_pass_rate` | Non-review task JSON files | Fraction of non-review tasks whose `state == "success"`. |
| 5 | `review_quality` | All task JSON files | Average `confidence` across all tasks (review and non-review). |
| 6 | `change_complexity` | Task file count | Inverse of task count: `1 / (1 + N)`. More tasks → lower score. Note: Sprint 1–4 analysis showed this is weakly anti-correlated with quality, so v2 weights reduce it to 0.02. |
| 7 | `review_catch_value` | `review_outcomes` DB table | Normalised score reflecting review phase value: fix verification rate, severity-weighted catch rate, false-positive penalty. Only present when review outcomes exist. See [§9](#9-review-catch-value). |
| 8 | `adversarial_audit` | `AuditResult` records | Average `reviewer_accuracy_score` across adversarial audit results. Only present when audit is enabled. See [§10](#10-adversarial-audit). |
| 9 | `historical_calibration` | Injected via `extra_signals` | Longitudinal reviewer accuracy from `ReviewerCalibrator`. Caller-provided. See [§11](#11-reviewer-calibration). |

### Signal File Formats

**`acceptance_results.json`** (written by the acceptance-test phase):
```json
{
  "passed": 12,
  "failed": 1,
  "errors": 0,
  "total": 13,
  "pass_rate": 0.923
}
```

**`code_quality_results.json`** (written by the code-quality phase):
```json
{
  "passed": 5,
  "failed": 0,
  "total": 5,
  "pass_rate": 1.0
}
```

**Task result JSON** (per-phase output, e.g. `implement.json`):
```json
{
  "task_type": "code",
  "state": "success",
  "confidence": 0.92,
  "result": { "text": "..." }
}
```

---

## 3. Weight Tables

Two weight tables are provided. Weights do **not** need to sum to 1.0 — the `_weighted_average` method renormalises over present signals.

### v1 Weights (`DEFAULT_WEIGHTS`)

Backward-compatible weights:

| Signal | Weight | Notes |
|---|---|---|
| `acceptance_pass_rate` | **0.40** | Primary (Issue #528) |
| `llm_judge` | 0.30 | |
| `test_pass_rate` | 0.20 | |
| `code_quality` | 0.20 | Issue #533 |
| `review_catch_value` | 0.15 | Issue #4.1.3 |
| `review_quality` | 0.10 | |
| `change_complexity` | 0.10 | |
| `adversarial_audit` | 0.10 | Issue #4.1.4 |
| `historical_calibration` | 0.05 | Extra-signals only |

### v2 Weights (`DEFAULT_WEIGHTS_V2`)

Calibrated with Sprint 1–4 data (Issue #429.1):

| Signal | Weight | Change | Rationale |
|---|---|---|---|
| `acceptance_pass_rate` | **0.40** | — | Primary behavioral signal |
| `llm_judge` | **0.40** | ↑ 0.30→0.40 | Most discriminative quality signal (0.97+ on good runs) |
| `test_pass_rate` | 0.30 | ↑ 0.20→0.30 | Binary reliability, very trustworthy |
| `code_quality` | 0.20 | — | Code quality checks |
| `review_catch_value` | 0.12 | ↓ 0.15→0.12 | Often absent in coding pipelines |
| `adversarial_audit` | 0.08 | ↓ 0.10→0.08 | Rarely present in Sprint 1–4 |
| `review_quality` | 0.04 | ↓ 0.10→0.04 | Re-added at reduced weight |
| `change_complexity` | 0.02 | ↓ 0.10→0.02 | Task count ≠ quality indicator |
| `historical_calibration` | 0.02 | ↓ 0.05→0.02 | Extra-signals only |

---

## 4. Composite Score Calculation

The composite score is a **renormalised weighted average** over all present signals:

$$\text{composite} = \frac{\sum_{i \in \text{present}} w_i \cdot v_i}{\sum_{i \in \text{present}} w_i}$$

Where:
- $v_i$ = signal value (clamped to [0, 1])
- $w_i$ = signal weight from the effective weight table

**Key properties:**
- Absent signals are simply excluded — they don't drag the score down.
- Extra signals (injected via `extra_signals` parameter) participate in the same average.
- When no signals are extracted, the score defaults to 0.0 (ConfidenceLevel.LOW).
- When all weights are 0, falls back to an unweighted mean.

### Data Structures

```python
@dataclass
class ConfidenceSignal:
    name: str        # e.g. "llm_judge"
    value: float     # Normalised [0, 1] — auto-clamped
    weight: float    # Non-negative
    raw_value: Any   # Original un-normalised data
    source: str      # Human-readable origin

@dataclass
class ConfidenceResult:
    signals: list[ConfidenceSignal]
    composite_score: float            # [0, 1]
    confidence_level: ConfidenceLevel # HIGH / MEDIUM / LOW
    explanation: str                  # Multi-line human-readable breakdown
```

---

## 5. Confidence Levels

The composite score maps to a 3-tier `ConfidenceLevel` enum:

| Level | Score Range | Description |
|---|---|---|
| **HIGH** | ≥ 0.90 | Eligible for auto-merge |
| **MEDIUM** | ≥ 0.75 | Acceptable but may need review |
| **LOW** | < 0.75 | Needs retry or rejection |

> **Note:** This is distinct from `schemas.ConfidenceLevel`, which is a 5-tier enum (VERY_LOW → VERY_HIGH) scoped to individual task results. The 3-tier enum here is scoped to full pipeline runs.

---

## 6. Routing Engine

The `RoutingEngine` maps a `ConfidenceResult` to a `RoutingDecision` by evaluating the composite score against an ordered list of `RoutingTier` definitions.

### Default Routing Tiers

| Tier | Score Range | Strategy | Max Retries |
|---|---|---|---|
| `auto_merge` | [0.90, 1.01) | `merge` | — |
| `queue_review` | [0.70, 0.90) | `queue_review` | — |
| `retry` | [0.50, 0.70) | `retry` | 2 |
| `reject` | [0.00, 0.50) | `reject` | — |

**Thresholds** (authoritative, defined in `confidence.py`):
- `AUTO_MERGE_THRESHOLD = 0.90`
- `HUMAN_REVIEW_THRESHOLD = 0.70` (lowered from 0.75 post-calibration, Issue #429.1)

### Evaluation Rules

1. Tiers are sorted by `min_score` **descending** — the highest-threshold tier is tested first.
2. A tier matches when `min_score <= score < max_score` (upper bound exclusive).
3. The first matching tier wins.
4. If no tier matches → `"unrouted"` fallback with strategy `"review"`.

### RoutingDecision

```python
@dataclass(frozen=True)
class RoutingDecision:
    tier: str                      # e.g. "auto_merge" or "unrouted"
    score: float                   # The composite score evaluated
    confidence_level: ConfidenceLevel
    strategy: str                  # "merge", "queue_review", "retry", "reject"
    matched: bool                  # True if a tier was matched
    requires: list[str]            # Preconditions (e.g. ["approve_verdict"])
    notify: list[str]              # Notification targets
    max_retries: int               # 0 except for retry tier
```

### RoutingTier

Each tier is defined as:

```python
@dataclass(frozen=True)
class RoutingTier:
    name: str                      # Unique tier name
    min_score: float               # Inclusive lower bound [0, 1]
    max_score: float               # Exclusive upper bound (may be >1.0)
    requires: list[str]            # Preconditions
    notify: list[str]              # Notification targets
    strategy: str                  # Action verb
    max_retries: int               # For retry tiers (clamped ≥ 0)
```

### Threshold Validation

`RoutingEngine.validate_thresholds()` checks for:
- Duplicate tier names
- Coverage gaps (scores that match no tier)
- Overlapping tiers
- Missing coverage at 0.0 or 1.0

---

## 7. Trust-Profile Dynamic Routing

`RoutingEngine.evaluate()` optionally consults a trust profile to dynamically adjust routing thresholds.

**When active:** If `repo`, `template_id`, `task_type`, and `db` are all provided, and the trust profile has accumulated ≥ `bootstrap_threshold` (default 10) successful merges, the routing config is rebuilt from the profile's calibrated thresholds:

```
auto_merge_threshold   → from trust_profiles.auto_merge_threshold
human_review_threshold → from trust_profiles.human_review_threshold
```

**Trust-derived tier layout:**

| Tier | Score Range | Strategy |
|---|---|---|
| `auto_merge` | [profile.auto_merge_threshold, 1.01) | `merge` |
| `queue_review` | [profile.human_review_threshold, auto_merge_threshold) | `queue_review` |
| `retry` | [0.50, human_review_threshold) | `retry` (max_retries=2) |
| `reject` | [0.00, 0.50) | `reject` |

When `human_review_threshold ≤ 0.50`, the retry tier collapses and everything below the review threshold is rejected.

**Fallback:** If the trust profile doesn't exist, hasn't passed bootstrap, or lookup fails, standard `DEFAULT_ROUTING_CONFIG` is used.

---

## 8. Dynamic Weight Calibration

When `calibration_outcomes` (a list of historical `ReviewOutcome` dicts) is passed to `compute_confidence()`, the calculator dynamically adjusts signal weights based on reviewer accuracy:

1. Pass outcomes to `ReviewerCalibrator.compute()` → per-model `CalibrationMetrics`.
2. Identify the **primary model** (highest `total_reviews` with non-null `overall_accuracy`).
3. Scale the `llm_judge` weight:
   - `scaled_weight = base_weight × (0.5 + accuracy)`
   - When `accuracy = 0.0` → weight halved (unreliable reviewer)
   - When `accuracy = 1.0` → weight × 1.5 (highly accurate reviewer)
4. Redistribute the weight delta proportionally across all other signals.
5. Renormalise so all weights sum to 1.0.

**Fallback:** If calibration fails, static weights are used unchanged.

---

## 9. Review Catch Value

The `review_catch_value` signal measures whether the review phase delivered real value. Computed by `ReviewCatchValueCalculator` from `review_outcomes` DB records.

### Sub-scores

| Sub-score | Weight | Formula |
|---|---|---|
| `fix_verification_rate` | 0.40 | Fraction of outcomes where `fix_verified = True` |
| `weighted_catch_rate` | 0.40 | Severity-weighted fraction of issue mass confirmed by verified fixes |
| `false_positive_penalty` | 0.20 | `1.0 - false_positive_rate` (issues + APPROVE verdict = false positive) |

$$\text{review\_catch\_value} = 0.40 \times \text{fix\_verification\_rate} + 0.40 \times \text{weighted\_catch\_rate} + 0.20 \times (1.0 - \text{false\_positive\_rate})$$

### Severity Weights

Used to compute `weighted_catch_rate`:

| Severity | Weight |
|---|---|
| BLOCKER | 1.00 |
| MAJOR | 0.75 |
| MINOR | 0.25 |
| NITPICK | 0.10 |
| Unknown | 0.10 |

**Empty outcomes:** Returns a neutral score of **0.5** — "no data" is neither good nor bad.

---

## 10. Adversarial Audit

The adversarial audit is a **post-pipeline second-opinion review** (not an inline phase). It re-reviews the code using a different model or security-focused prompt.

**Purpose:**
1. Catch issues the original reviewer missed (false negatives)
2. Detect false approvals (APPROVE + real problems)
3. Surface security gaps

**Signal:** `adversarial_audit` = average `reviewer_accuracy_score` across audit results.

The `reviewer_accuracy_score` measures what fraction of audit-found issues the original reviewer also caught.

**`AuditResult` structure:**

| Field | Type | Description |
|---|---|---|
| `run_id` | str | Pipeline run ID |
| `audit_verdict` | str | `APPROVE` or `REQUEST_CHANGES` |
| `caught_issues` | list[AuditIssue] | Issues found by the auditor |
| `reviewer_accuracy_score` | float | Fraction of issues the original reviewer caught [0, 1] |
| `false_approval` | bool | Original said APPROVE but audit found real issues |

**Activation:** Pass `audit=True` to `run_scoring()`, or invoke `AuditPhase.run()` directly.

---

## 11. Reviewer Calibration

`ReviewerCalibrator` computes longitudinal accuracy metrics per reviewer model from historical `review_outcomes`.

### Metrics

| Metric | Formula | Description |
|---|---|---|
| `approve_accuracy` | `approve_held_up / approve_count` | How often APPROVEs were correct (no fix needed) |
| `request_changes_accuracy` | `rc_valid / rc_count` | How often REQUEST_CHANGES were confirmed real |
| `overall_accuracy` | `(held_up + rc_valid) / total` | Combined accuracy |

**Edge case:** When the denominator is 0, the metric is `None` (not 0.0 — "no data" ≠ "always wrong").

### Usage

```python
from orchestration_engine.reviewer_calibration import ReviewerCalibrator

outcomes = db.list_review_outcomes(limit=500)
calibrator = ReviewerCalibrator(db=db, aggregation_window="all-time")
metrics_map = calibrator.calibrate_and_save(outcomes)

for model, m in metrics_map.items():
    print(f"{model}: accuracy={m.overall_accuracy}")
```

`calibrate_and_save()` persists snapshots to the `reviewer_calibration` DB table for historical tracking.

---

## 12. Scenario-Based Scoring

`run_scoring()` in `scoring.py` is the entry point invoked after every pipeline run (when the template has a `scenario:` field).

**Flow:**
1. Resolve scenario YAML file (relative to template directory or cwd).
2. Load `ScenarioRunner` from `scenario_runner/`.
3. Read phase outputs from the output directory (`_final_output.json` + per-phase JSON files).
4. Run grader criteria defined in the scenario against the outputs.
5. Print a rich score report (per-criterion table + summary).
6. Optionally run adversarial audit (`audit=True`).
7. Return `(passed: bool, weighted_score: float)`.

**Output structure assembled for grading:**
```python
{
    "final":  <_final_output.json contents>,
    "phases": {
        "research": <research.json contents>,
        "implement": <implement.json contents>,
        ...
    }
}
```

---

## 13. Template Configuration

Templates can customise routing via the `routing_config` field:

```yaml
routing_config:
  tiers:
    - name: auto_merge
      min_score: 0.95
      max_score: 1.01
      strategy: merge
      requires:
        - approve_verdict
      notify:
        - slack:dev-team

    - name: queue_review
      min_score: 0.80
      max_score: 0.95
      strategy: queue_review
      notify:
        - email:lead@example.com

    - name: retry
      min_score: 0.50
      max_score: 0.80
      strategy: retry
      max_retries: 3

    - name: reject
      min_score: 0.00
      max_score: 0.50
      strategy: reject
```

**Per-tier fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Unique tier name |
| `min_score` | float | required | Inclusive lower bound [0, 1] |
| `max_score` | float | required | Exclusive upper bound (use 1.01 for top tier) |
| `strategy` | string | `"review"` | Action: `merge`, `queue_review`, `retry`, `reject`, or custom |
| `requires` | list | `[]` | Preconditions (e.g. `["approve_verdict"]`) |
| `notify` | list | `[]` | Notification targets |
| `max_retries` | int | `0` | Max retries (for `retry` strategy) |

When `routing_config` is absent, `DEFAULT_ROUTING_CONFIG` is used.

---

## Quick Reference

| Score | Default Tier | Action | Run Status |
|---|---|---|---|
| ≥ 0.90 | `auto_merge` | Merge automatically | `success` |
| 0.70 – 0.89 | `queue_review` | Queue for human review | `pending_review` |
| 0.50 – 0.69 | `retry` | Retry up to 2 times | `pending` (re-queued) |
| < 0.50 | `reject` | Reject the run | `failed` |

**Authoritative thresholds:**
- `AUTO_MERGE_THRESHOLD = 0.90`
- `HUMAN_REVIEW_THRESHOLD = 0.70`

Both are defined in `confidence.py` and imported by `routing.py`.
