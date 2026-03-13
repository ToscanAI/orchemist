# Trust Calibration

> **Source module:** [`trust.py`](../src/orchestration_engine/trust.py)
> **DB tables:** `trust_profiles`, `trust_adjustments` (see [database-schema.md](database-schema.md))
> **Related:** [confidence-scoring.md](confidence-scoring.md) (routing integration)

Trust calibration is a feedback loop that adjusts auto-merge thresholds based on real-world outcomes. Each `(repo, template_id, task_type)` triplet has its own trust profile with an EMA-updated trust score. As the score rises, auto-merge thresholds relax; as it falls (regressions, reverts), thresholds tighten.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Trust Profile](#2-trust-profile)
3. [Trust Config](#3-trust-config)
4. [EMA Update Rule](#4-ema-update-rule)
5. [Threshold Derivation](#5-threshold-derivation)
6. [Bootstrap Guard](#6-bootstrap-guard)
7. [Outcome Types](#7-outcome-types)
8. [Idle-Profile Decay](#8-idle-profile-decay)
9. [Integration Points](#9-integration-points)
10. [REST API](#10-rest-api)
11. [Worked Examples](#11-worked-examples)

---

## 1. Overview

```
Pipeline completes
       │
       ▼
┌──────────────────┐     ┌───────────────────┐     ┌──────────────────┐
│ Confidence       │────→│ Routing Engine     │────→│ Action           │
│ Calculator       │     │ .evaluate()        │     │ (merge/review/…) │
└──────────────────┘     └───────┬────────────┘     └────────┬─────────┘
                                 │                           │
                                 │ reads thresholds          │ outcome feedback
                                 ▼                           ▼
                         ┌───────────────────┐     ┌──────────────────┐
                         │ Trust Profile     │◄────│ Trust Calibrator  │
                         │ (DB row)          │     │ .update_after_run │
                         └───────────────────┘     └──────────────────┘
                                                           │
                                                           ▼
                                                   ┌──────────────────┐
                                                   │ Trust Adjustments│
                                                   │ (audit log)      │
                                                   └──────────────────┘
```

**Cycle:**
1. `RoutingEngine.evaluate()` reads the trust profile's `auto_merge_threshold` and `human_review_threshold` to build a dynamic routing config.
2. After the pipeline action completes (merge, regression, revert), `TrustCalibrator.update_after_run()` applies an EMA update to the trust score.
3. A new `auto_merge_threshold` is derived from the updated score.
4. Every change is logged in `trust_adjustments` for auditing.

---

## 2. Trust Profile

The `TrustProfile` dataclass mirrors the `trust_profiles` DB table. One row per unique `(repo, template_id, task_type)` triplet.

| Field | Type | Default | Description |
|---|---|---|---|
| `repo` | str | required | Git repository slug (e.g. `"owner/repo"`) |
| `template_id` | str | required | Pipeline template ID |
| `task_type` | str | required | Task classification (e.g. `"bugfix"`, `"feature"`, `"general"`) |
| `auto_merge_threshold` | float | `0.85` | Minimum confidence for auto-merge |
| `human_review_threshold` | float | `0.70` | Minimum confidence to skip review queue |
| `trust_score` | float | `0.5` | Current trust score [0.0, 1.0] |
| `total_runs` | int | `0` | Total pipeline runs |
| `successful_merges` | int | `0` | Auto-merges without revert |
| `regressions` | int | `0` | Regressions after auto-merge |
| `reverted_prs` | int | `0` | PRs reverted after auto-merge |
| `last_run_at` | str \| None | None | ISO-8601 UTC timestamp of most recent run |
| `id` | int \| None | None | Auto-assigned DB primary key |

**Unique constraint:** `UNIQUE(repo, template_id, task_type)` — upsert on conflict.

---

## 3. Trust Config

`TrustConfig` holds algorithm hyper-parameters. Not persisted to the DB — passed at call-time or embedded in higher-level config.

| Parameter | Default | Description |
|---|---|---|
| `success_delta` | `+0.02` | Score increase after successful auto-merge |
| `regression_penalty` | `-0.10` | Score decrease after regression |
| `revert_penalty` | `-0.15` | Score decrease after PR revert |
| `min_score` | `0.0` | Lower bound for trust score |
| `max_score` | `1.0` | Upper bound for trust score |
| `initial_score` | `0.5` | Starting score for new profiles |
| `initial_auto_merge_threshold` | `0.85` | Default auto-merge threshold |
| `initial_human_review_threshold` | `0.70` | Default human-review threshold |

> **Note:** `TrustConfig` defines the *deltas* used by the legacy additive model. `TrustCalibrator` uses an EMA model with `OUTCOME_SCORES` constants instead. Both coexist — `TrustConfig` is available for callers that prefer explicit deltas.

---

## 4. EMA Update Rule

`TrustCalibrator` uses an Exponential Moving Average to update the trust score after each pipeline outcome.

### Formula

$$\text{new\_score} = \text{clamp}\Big(\alpha \times S_{\text{outcome}} + (1 - \alpha) \times \text{old\_score},\ 0.0,\ 1.0\Big)$$

Where:
- $\alpha$ = smoothing factor (default `0.1`). Higher = faster reaction to recent outcomes.
- $S_{\text{outcome}}$ = raw outcome score from `OUTCOME_SCORES` table.

### Outcome Scores

| Outcome | $S_{\text{outcome}}$ | Description |
|---|---|---|
| `run_success` | `+1.0` | Successful pipeline completion / auto-merge |
| `regression` | `-3.0` | CI regression detected after merge |
| `revert` | `-2.0` | PR reverted after auto-merge |
| `human_override_reject` | `-1.0` | Human reviewer rejected after auto-merge routing |

### EMA Behaviour

With `α = 0.1`:

| Starting Score | Outcome | Calculation | New Score |
|---|---|---|---|
| 0.50 | `run_success` (+1.0) | 0.1×1.0 + 0.9×0.50 | **0.55** |
| 0.80 | `run_success` (+1.0) | 0.1×1.0 + 0.9×0.80 | **0.82** |
| 0.80 | `regression` (-3.0) | 0.1×(-3.0) + 0.9×0.80 = 0.42 | **0.42** |
| 0.80 | `revert` (-2.0) | 0.1×(-2.0) + 0.9×0.80 = 0.52 | **0.52** |
| 0.10 | `regression` (-3.0) | 0.1×(-3.0) + 0.9×0.10 = -0.21 → clamped | **0.00** |

**Key properties:**
- A single regression can drop the score by ~0.38 points (from 0.80 → 0.42).
- Recovery requires many consecutive successes (~15 to climb back from 0.42 → 0.80).
- The asymmetry is intentional — regressions are far more costly than incremental successes.

---

## 5. Threshold Derivation

After each score update, the `auto_merge_threshold` is re-derived from the trust score:

$$\text{threshold} = \text{conservative} - \text{trust\_score} \times (\text{conservative} - \text{aggressive})$$

Clamped to [0.0, 1.0].

### Default Parameters

| Parameter | Default | Description |
|---|---|---|
| `conservative` | `0.98` | Threshold when trust is low or during bootstrap |
| `aggressive` | `0.70` | Threshold at maximum trust (score = 1.0) |

### Threshold at Various Trust Scores

| Trust Score | Threshold | Effect |
|---|---|---|
| 0.00 | **0.98** | Almost nothing auto-merges |
| 0.25 | **0.91** | Very selective |
| 0.50 | **0.84** | Moderate — close to initial default |
| 0.75 | **0.77** | Increasingly permissive |
| 1.00 | **0.70** | Most permissive — matches `HUMAN_REVIEW_THRESHOLD` |

The linear interpolation means that as trust builds through repeated successes, the system progressively allows lower-confidence runs to auto-merge.

---

## 6. Bootstrap Guard

New profiles start with `trust_score = 0.5` and **zero** successful merges. The bootstrap guard prevents premature threshold relaxation:

- **During bootstrap** (`successful_merges < bootstrap_threshold`): threshold is locked at `conservative` (0.98).
- **After bootstrap** (`successful_merges >= bootstrap_threshold`): threshold is derived dynamically from the trust score.

| Parameter | Default | Description |
|---|---|---|
| `bootstrap_threshold` | `10` | Minimum successful merges before dynamic thresholds activate |

**Rationale:** A new pipeline has no track record. Locking the threshold at 0.98 during bootstrap means essentially nothing auto-merges — every run goes to human review until there's a sufficient history of successful outcomes.

---

## 7. Outcome Types

`TrustCalibrator.update_after_run()` accepts one of four outcome strings:

| Outcome | Score Effect | Counter Incremented | When Used |
|---|---|---|---|
| `run_success` | +1.0 (EMA) | `total_runs`, `successful_merges` | Pipeline completed successfully, auto-merged without issue |
| `regression` | -3.0 (EMA) | `total_runs`, `regressions` | CI regression detected after auto-merge |
| `revert` | -2.0 (EMA) | `total_runs`, `reverted_prs` | PR was reverted after auto-merge |
| `human_override_reject` | -1.0 (EMA) | `total_runs` | Human reviewer rejected a run that routing had queued |

**Validation:** Passing any other string raises `ValueError`.

### Update Steps

1. Validate outcome against `VALID_OUTCOMES`.
2. Load (or create) the trust profile from the DB.
3. Compute new score via EMA formula.
4. Increment appropriate counters.
5. Derive new `auto_merge_threshold` via `compute_threshold()`.
6. Persist updated profile via `db.upsert_trust_profile()`.
7. Record adjustment via `db.insert_trust_adjustment()`.

---

## 8. Idle-Profile Decay

`decay_idle_profiles()` applies weekly trust-score decay to profiles that haven't had a pipeline run in `threshold_days` or more.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `decay_rate` | `0.05` | Score reduction per full idle week |
| `threshold_days` | `7` | Days of inactivity before decay starts |
| `floor` | `0.30` | Minimum score — decay never pushes below this |

### Formula

$$\text{new\_score} = \max\big(\text{floor},\ \text{old\_score} - \text{decay\_rate} \times \text{weeks\_idle}\big)$$

### Behaviour

| Weeks Idle | Starting Score | Decay | New Score |
|---|---|---|---|
| 1 | 0.80 | -0.05 | **0.75** |
| 2 | 0.80 | -0.10 | **0.70** |
| 4 | 0.80 | -0.20 | **0.60** |
| 8 | 0.80 | -0.40 | **0.40** |
| 10 | 0.80 | -0.50 | **0.30** (floor) |

**After decay:**
- The `auto_merge_threshold` is re-derived from the new score.
- A `trust_adjustments` row is written with `reason = "idle_decay"`.
- Profiles already at or below the floor are skipped.
- Profiles with no `last_run_at` are treated as 1 week idle.

**Invocation:** Called periodically (e.g. via cron or a scheduled task). Not called automatically by the daemon — it's a maintenance function.

```python
from orchestration_engine.trust import decay_idle_profiles
from orchestration_engine.db import Database

db = Database()
results = decay_idle_profiles(db)
for r in results:
    print(f"Profile {r['profile_id']}: {r['old_score']:.2f} → {r['new_score']:.2f} "
          f"({r['weeks_idle']} weeks idle)")
```

---

## 9. Integration Points

### Daemon (post-pipeline routing)

The daemon calls `RoutingEngine.evaluate()` with `repo`, `template_id`, `task_type`, and `db` parameters. When a trust profile exists and has passed bootstrap, the routing engine dynamically adjusts thresholds:

```python
# In daemon.py — _compute_and_dispatch_routing()
decision = RoutingEngine(_routing_cfg).evaluate(
    confidence_result,
    repo=_trust_repo,
    template_id=_trust_template_id,
    task_type=_trust_task_type,
    db=db,
)
```

### Regression Webhook Handler

When a CI regression is detected, `RegressionWebhookHandler` applies a trust penalty:

```python
# In regression.py
calibrator = TrustCalibrator(
    repo=self._repo_slug,
    template_id=self._template_id,
    task_type=regression.failure_type or "ci_failure",
)
calibrator.update_after_run(
    run_id=regression.id,
    outcome="regression",
    db=self._db,
)
```

### REST API Manual Override

Operators can manually override a trust score via `PUT /api/v1/trust/profiles/{id}`:

```json
{
  "trust_score": 0.75,
  "reason": "Lowered after regression incident",
  "reviewed_by": "conny"
}
```

This writes a `trust_adjustments` row with `reason = "manual_override:conny"`.

---

## 10. REST API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/trust/profiles` | List all trust profiles |
| `GET` | `/api/v1/trust/profiles/{id}` | Get a single profile |
| `PUT` | `/api/v1/trust/profiles/{id}` | Manual score override |
| `GET` | `/api/v1/trust/adjustments?profile_id=N` | Audit log for a profile |

See [rest-api-v1.md](rest-api-v1.md#7-trust-profiles) for full request/response schemas.

---

## 11. Worked Examples

### Example 1: New Pipeline Ramping Up

A new `(myorg/myrepo, coding-pipeline-v1, bugfix)` profile:

| Run | Outcome | Trust Score | Threshold | Merges |
|---|---|---|---|---|
| — | *created* | 0.500 | **0.980** (bootstrap) | 0 |
| 1 | `run_success` | 0.550 | **0.980** (bootstrap) | 1 |
| 2 | `run_success` | 0.595 | **0.980** (bootstrap) | 2 |
| 5 | `run_success` | 0.715 | **0.980** (bootstrap) | 5 |
| 10 | `run_success` | 0.814 | **0.980** (bootstrap) | 10 |
| 11 | `run_success` | 0.833 | **0.747** (dynamic!) | 11 |
| 20 | `run_success` | 0.903 | **0.727** | 20 |

After 10 successful merges the bootstrap guard lifts and thresholds start relaxing.

### Example 2: Regression Impact

A mature profile with trust_score = 0.85 and 25 successful merges:

| Event | Trust Score | Threshold | Notes |
|---|---|---|---|
| Steady state | 0.850 | 0.742 | 25 merges, dynamic routing |
| `regression` | 0.465 | 0.850 | Single regression drops score by 0.385 |
| `run_success` | 0.519 | 0.835 | Slow climb back |
| 5× `run_success` | 0.678 | 0.790 | Still recovering |
| 10× `run_success` | 0.785 | 0.760 | Nearly recovered |

**Key insight:** One regression takes ~15 consecutive successes to recover from, enforcing caution in the system.

### Example 3: Idle Decay

A profile idle for 6 weeks with trust_score = 0.80:

| Week | Trust Score | Threshold | Notes |
|---|---|---|---|
| 0 (last run) | 0.800 | 0.756 | Active |
| 1 | 0.800 | 0.756 | Within threshold_days — no decay |
| 2 | 0.750 | 0.770 | First decay applied |
| 4 | 0.650 | 0.798 | Threshold tightening |
| 6 | 0.550 | 0.826 | Approaching conservative |
| 10 | 0.350 | 0.882 | Near floor |
| 11 | 0.300 | 0.896 | Floor reached — no further decay |

---

## Configuration Reference

### TrustCalibrator Constructor

```python
TrustCalibrator(
    repo="owner/repo",
    template_id="coding-pipeline-v1",
    task_type="bugfix",
    alpha=0.1,              # EMA smoothing (0, 1]
    conservative=0.98,      # Threshold at low trust / bootstrap
    aggressive=0.70,        # Threshold at max trust
    bootstrap_threshold=10, # Merges required before dynamic thresholds
)
```

### Programmatic Usage

```python
from orchestration_engine.trust import TrustCalibrator
from orchestration_engine.db import Database

db = Database()
calibrator = TrustCalibrator(
    repo="ToscanAI/orchestration-engine",
    template_id="coding-pipeline-v1",
    task_type="bugfix",
)

# Record a successful merge
result = calibrator.update_after_run(
    run_id="run-abc-123",
    outcome="run_success",
    db=db,
)

print(f"Score: {result['old_score']:.4f} → {result['new_score']:.4f}")
print(f"Threshold: {result['threshold']:.4f}")
print(f"Merges: {result['successful_merges']}")
```

### Return Value

`update_after_run()` returns:

```python
{
    "profile_id":        1,
    "adjustment_id":     42,
    "run_id":            "run-abc-123",
    "outcome":           "run_success",
    "old_score":         0.5000,
    "new_score":         0.5500,
    "delta":             0.0500,
    "threshold":         0.9800,  # bootstrap locked
    "total_runs":        1,
    "successful_merges": 1,
    "regressions":       0,
    "reverted_prs":      0,
}
```
