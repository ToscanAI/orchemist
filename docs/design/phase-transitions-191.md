# Phase Transitions — Architecture Design (#191)

**Author:** Architecture Sub-agent  
**Date:** 2026-02-28  
**Original Status:** Draft — Pending Review  
**Current Status:** ✅ **IMPLEMENTED** — Phase transitions are fully operational in `transitions.py` and `StateMachineSequencer`. The `PhaseOutcome` enum routes on success/failed/timeout/skipped outcomes. See also `ARCHITECTURE.md` for the current state-machine execution model.  
**Complexity:** High (most complex feature to date)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current Architecture](#2-current-architecture)
3. [Data Model Changes](#3-data-model-changes)
4. [Execution Model](#4-execution-model)
5. [Loop Detection Algorithm](#5-loop-detection-algorithm)
6. [Interaction with Parallel Execution](#6-interaction-with-parallel-execution)
7. [YAML Template Syntax](#7-yaml-template-syntax)
8. [Edge Cases and Failure Modes](#8-edge-cases-and-failure-modes)
9. [Migration Path](#9-migration-path)
10. [Issue Breakdown](#10-issue-breakdown)
11. [Risk Assessment](#11-risk-assessment)

---

## 1. Executive Summary

Issue #191 extends the orchestration engine from a fixed DAG executor to a **conditional state machine**. Phases can declare `transitions` that map execution outcomes (`success`, `failed`, `timeout`, `skipped`) to next-phase IDs, enabling branching, recovery paths, and iterative loops.

**Design principles:**
- **Backward compatible:** Phases without `transitions` behave exactly as today (DAG topological sort)
- **Hybrid model:** A pipeline can mix DAG phases and transition-based phases
- **Safety first:** Infinite loop detection with configurable `max_iterations`
- **Minimal surface area:** New class `StateMachineSequencer` extends `PhaseSequencer`; existing code untouched

---

## 2. Current Architecture

### Execution Model (today)

```
PipelineTemplate → TemplateEngine.get_execution_order() → List[List[str]] (waves)
                                                              ↓
PhaseSequencer.execute() iterates waves:
  Wave 0: [phase_a, phase_b]  ← parallel if template.parallel=True
  Wave 1: [phase_c]           ← depends on a & b
  Wave 2: [phase_d]           ← depends on c
```

Key characteristics:
- **Kahn's algorithm** produces topological waves from `depends_on` edges
- **Parallel execution** within waves via `ThreadPoolExecutor`
- **Fail-fast** aborts pipeline on any phase failure
- **Retry logic** is phase-level (re-invokes executor N times)
- **No branching** — every phase in the DAG always executes

### Key Classes

| Class | Role |
|-------|------|
| `PhaseDefinition` | Dataclass — phase config (prompt, model, retries, etc.) |
| `PipelineTemplate` | Dataclass — list of phases + pipeline-level config |
| `TemplateEngine` | Loads YAML, computes execution order (Kahn's algorithm) |
| `PhaseSequencer` | Executes phases sequentially/parallel, handles retries |

### Current Phase Outcomes

The sequencer currently recognizes these result states:
- `success` — `TaskState.SUCCESS` from executor
- `failed` / `permanently_failed` — triggers pipeline abort
- `skipped` — synthetic state from parallel abort_event

There is no `timeout` state — timeouts surface as `failed` via `timeout_seconds` on `TaskSpec`.

---

## 3. Data Model Changes

### 3.1 PhaseOutcome Enum (new)

```python
# schemas.py
class PhaseOutcome(str, Enum):
    """Outcomes that can trigger phase transitions."""
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    # Extensible: add custom outcomes as needed
```

**Why an enum?** Validates transition keys at template-load time. Custom outcomes can be added by extending the enum or by accepting arbitrary strings with a warning.

### 3.2 PhaseDefinition — New Fields

```python
@dataclass
class PhaseDefinition:
    # ... existing fields ...
    
    # NEW: Phase transition routing
    transitions: Dict[str, str] = field(default_factory=dict)
    """Maps PhaseOutcome value → next phase ID. Empty dict = use DAG behavior."""
    
    # NEW: Iteration limit for loops
    max_iterations: int = 0
    """Max times this phase can execute in a single pipeline run.
    0 = no limit (use pipeline-level default). Provides per-phase override."""
```

The `transitions` dict maps outcome strings to phase IDs:

```python
transitions = {
    "success": "next_phase",
    "failed": "error_handler",
    "timeout": "fallback_phase",
}
```

**Design decision — why `Dict[str, str]` not a more complex type:**
- Simple, YAML-friendly, easy to validate
- Outcome keys are just strings (validated against `PhaseOutcome` + custom)
- Values are phase IDs (validated against template's phase list)
- No need for conditional expressions in v1 — outcome routing is sufficient

### 3.3 PipelineTemplate — New Fields

```python
@dataclass
class PipelineTemplate:
    # ... existing fields ...
    
    # NEW: Pipeline-level transition defaults
    max_iterations: int = 10
    """Default max times any single phase can execute via transitions.
    Prevents infinite loops. Per-phase max_iterations overrides this."""
    
    default_transitions: Dict[str, str] = field(default_factory=dict)
    """Default transitions applied to ALL phases that don't define their own.
    Useful for global error handling (e.g., failed → error_handler)."""
```

### 3.4 Outcome Determination Logic

A phase's outcome is determined from the `TaskResult`:

```python
def _determine_outcome(self, result: dict, phase: PhaseDefinition) -> PhaseOutcome:
    """Map a phase execution result to a PhaseOutcome."""
    state = result.get("state", "unknown")
    
    # Check for timeout (new: detect from error codes or metadata)
    errors = result.get("errors", [])
    for err in errors:
        code = err.get("code", "") if isinstance(err, dict) else getattr(err, "code", "")
        if code in ("TIMEOUT", "EXEC_TIMEOUT"):
            return PhaseOutcome.TIMEOUT
    
    if state == TaskState.SUCCESS.value:
        return PhaseOutcome.SUCCESS
    elif state in ("failed", "permanently_failed"):
        return PhaseOutcome.FAILED
    elif state == "skipped":
        return PhaseOutcome.SKIPPED
    else:
        # Unknown state → treat as failed for safety
        return PhaseOutcome.FAILED
```

---

## 4. Execution Model

### 4.1 StateMachineSequencer (new class)

```python
class StateMachineSequencer(PhaseSequencer):
    """Extends PhaseSequencer with conditional phase transitions.
    
    Execution modes:
    1. DAG mode (default): phases without transitions execute in topological order
    2. Transition mode: phases with transitions route to next phase based on outcome
    3. Hybrid: a pipeline can mix both — DAG phases run first, then transition 
       phases take over from a designated entry point
    """
```

**Why extend, not replace?** 
- `PhaseSequencer` has ~600 lines of battle-tested code (retries, parallel waves, file writes, callbacks)
- All of that must be preserved
- The state machine adds a new outer loop around the existing wave execution
- Extending means zero risk to existing pipelines

### 4.2 Execution Algorithm

```
StateMachineSequencer.execute(initial_input):
    1. Classify phases:
       - dag_phases: phases WITHOUT transitions (use current behavior)
       - transition_phases: phases WITH transitions
    
    2. If NO transition_phases exist:
       → delegate entirely to super().execute() (backward compat, zero overhead)
    
    3. If transition_phases exist:
       a. Execute all dag_phases first (using existing wave logic)
       b. Find transition entry point(s):
          - Phases with transitions that depend on dag_phases (or have no depends_on)
       c. Enter state machine loop:
          current_phase = entry_point
          iteration_counts = defaultdict(int)  # phase_id → execution count
          
          WHILE current_phase is not None:
              phase = get_phase(current_phase)
              
              # Loop detection
              iteration_counts[current_phase] += 1
              effective_max = phase.max_iterations or template.max_iterations
              if iteration_counts[current_phase] > effective_max:
                  → abort with MAX_ITERATIONS_EXCEEDED error
              
              # Execute phase (reuse existing _execute_wave_sequential logic)
              result = execute_single_phase(phase, initial_input)
              
              # Determine outcome
              outcome = _determine_outcome(result, phase)
              
              # Route to next phase
              transitions = phase.transitions or template.default_transitions
              if outcome.value in transitions:
                  current_phase = transitions[outcome.value]
              elif not transitions:
                  current_phase = None  # end of transition chain
              else:
                  # Outcome not in transitions map → end pipeline
                  # (configurable: could also abort/error)
                  current_phase = None
          
       d. Return combined results from DAG + transition phases
```

### 4.3 Phase Execution Reuse

The key insight is that `_execute_wave_sequential` already handles a single phase perfectly:
- Builds prompt with `_build_phase_input`
- Submits task, waits for result
- Handles retries via `_execute_and_wait`
- Writes files, invokes callbacks
- Stores result in `phase_outputs`

The state machine only needs to:
1. Choose **which** phase to execute next (routing)
2. Track iteration counts (loop detection)
3. Handle the new `timeout` outcome mapping

### 4.4 Re-execution of Phases (Loops)

When a transition routes back to an already-executed phase (loop), the phase re-executes with:
- Fresh prompt built from current `phase_outputs` (which now contains results from the previous iteration)
- Its own `retries` config applies independently per execution
- `phase_outputs[phase_id]` is **overwritten** with the latest result

**Why overwrite?** 
- Keeps `phase_outputs` simple (dict, not list)
- Downstream phases always see the latest iteration
- Full history available in `iteration_history` (new metadata field)

```python
# Track iteration history for observability
self.iteration_history: Dict[str, List[dict]] = defaultdict(list)

# Before overwriting phase_outputs:
if phase_id in self.phase_outputs:
    self.iteration_history[phase_id].append(self.phase_outputs[phase_id])
```

---

## 5. Loop Detection Algorithm

### 5.1 Static Analysis (Template Load Time)

At template validation time, detect potential loops:

```python
def _detect_transition_cycles(self, template: PipelineTemplate) -> List[str]:
    """Find cycles in the transition graph using DFS.
    
    Returns list of warning strings (not errors — loops are allowed 
    if max_iterations is set). Errors only if max_iterations=0 AND cycle exists.
    """
    # Build adjacency list from transitions
    graph: Dict[str, Set[str]] = defaultdict(set)
    for phase in template.phases:
        for outcome, target in phase.transitions.items():
            graph[phase.id].add(target)
    
    # DFS cycle detection
    visited = set()
    rec_stack = set()
    cycles = []
    
    def dfs(node, path):
        visited.add(node)
        rec_stack.add(node)
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                dfs(neighbor, path + [neighbor])
            elif neighbor in rec_stack:
                cycle = path[path.index(neighbor):] + [neighbor]
                cycles.append(cycle)
        rec_stack.discard(node)
    
    for phase_id in graph:
        if phase_id not in visited:
            dfs(phase_id, [phase_id])
    
    return cycles
```

**Validation rules:**
- Cycle detected + all phases in cycle have `max_iterations > 0` → **WARNING** (intentional loop)
- Cycle detected + any phase has `max_iterations = 0` + pipeline `max_iterations = 0` → **ERROR** (infinite loop risk)
- Cycle detected + pipeline `max_iterations > 0` → **WARNING** (pipeline default protects)

### 5.2 Runtime Enforcement

```python
# In the state machine loop:
iteration_counts[current_phase] += 1
effective_max = phase.max_iterations if phase.max_iterations > 0 else template.max_iterations

if effective_max > 0 and iteration_counts[current_phase] > effective_max:
    logger.error(
        f"Phase '{current_phase}' exceeded max_iterations ({effective_max}). "
        f"Aborting pipeline to prevent infinite loop."
    )
    return {
        "phase_outputs": self.phase_outputs,
        "final_output": last_result,
        "failed_phase": current_phase,
        "aborted": True,
        "abort_reason": "MAX_ITERATIONS_EXCEEDED",
    }
```

### 5.3 Global Safety Net

Even without explicit cycles, a pipeline-level total step counter prevents pathological cases:

```python
total_steps = 0
MAX_TOTAL_STEPS = sum(
    (p.max_iterations or template.max_iterations or 10) 
    for p in template.phases if p.transitions
) * 2  # 2x safety margin

while current_phase is not None:
    total_steps += 1
    if total_steps > MAX_TOTAL_STEPS:
        # Absolute safety net — should never trigger in well-configured pipelines
        abort("TOTAL_STEPS_EXCEEDED")
```

---

## 6. Interaction with Parallel Execution

### 6.1 Design Decision: Transitions Are Sequential

**Transitions only apply to sequential phase routing.** A phase with `transitions` is always executed as a single-phase sequential wave — never as part of a parallel wave.

**Rationale:**
- Transitions create a **linear state machine** — phase A's outcome determines phase B
- Parallel execution means "these phases are independent" — contradicts conditional routing
- Mixing transitions with parallel waves creates ambiguous semantics (which phase's outcome routes?)

### 6.2 Hybrid DAG + Transitions

A pipeline can have both DAG phases (parallel-capable) and transition phases (sequential):

```yaml
phases:
  # DAG phase — runs in parallel wave with other wave-0 phases
  - id: research
    depends_on: []
    # no transitions → DAG behavior
    
  # DAG phase — runs in parallel with research  
  - id: gather_data
    depends_on: []
    
  # Transition phase — runs after DAG completes
  - id: analyze
    depends_on: [research, gather_data]
    transitions:
      success: write_report
      failed: retry_analysis
      
  - id: retry_analysis
    transitions:
      success: write_report
      failed: escalate
```

**Execution order:**
1. Wave 0: `[research, gather_data]` — parallel (existing behavior)
2. Transition chain: `analyze → write_report` or `analyze → retry_analysis → ...`

### 6.3 Post-Wave Routing

After a parallel wave completes, if **any** phase in the wave has transitions, those transitions are evaluated:

```python
# After _execute_wave_parallel returns:
for phase_id in wave:
    phase = self._get_phase(phase_id)
    if phase.transitions:
        outcome = self._determine_outcome(self.phase_outputs[phase_id], phase)
        # Route based on outcome — enters state machine for this branch
```

**Important constraint:** If multiple phases in the same wave have transitions, their transition chains execute **sequentially** (one after another), not in parallel. This keeps the state machine deterministic.

---

## 7. YAML Template Syntax

### 7.1 Basic Transitions

```yaml
id: content-with-review
name: Content Pipeline with Review Loop
version: "1.1.0"
max_iterations: 5  # pipeline-level default

phases:
  - id: write_draft
    name: Write Draft
    model_tier: sonnet
    prompt_template: |
      Write an article about: {input[topic]}
    transitions:
      success: review
      
  - id: review
    name: Review Draft
    model_tier: opus
    depends_on: []  # not needed — transitions handle routing
    prompt_template: |
      Review this draft and respond with APPROVED or NEEDS_REVISION:
      {write_draft.output}
    transitions:
      success: publish     # reviewer approved
      failed: revise       # reviewer found issues
      
  - id: revise
    name: Revise Draft
    model_tier: sonnet
    max_iterations: 3     # max 3 revision rounds
    prompt_template: |
      Revise this draft based on review feedback:
      Draft: {write_draft.output}
      Feedback: {review.output}
    transitions:
      success: review      # back to review (loop!)
      failed: escalate
      
  - id: publish
    name: Publish
    model_tier: haiku
    prompt_template: |
      Format for publication: {write_draft.output}
      
  - id: escalate
    name: Escalate to Human
    model_tier: haiku
    human_review: true
    prompt_template: |
      This content could not be finalized after multiple attempts.
      Last draft: {write_draft.output}
      Last review: {review.output}
```

### 7.2 Error Recovery Pattern

```yaml
phases:
  - id: generate_code
    name: Generate Code
    model_tier: opus
    transitions:
      success: run_tests
      failed: generate_code_fallback
      timeout: generate_code_fallback
      
  - id: generate_code_fallback
    name: Generate Code (Fallback)
    model_tier: sonnet
    prompt_template: |
      The previous code generation failed. Try a simpler approach:
      {generate_code.output}
    transitions:
      success: run_tests
      failed: abort_with_report
      
  - id: run_tests
    name: Run Tests
    task_type: code
    transitions:
      success: deploy
      failed: fix_code
      
  - id: fix_code
    name: Fix Code
    max_iterations: 3
    transitions:
      success: run_tests   # retry the tests
      failed: abort_with_report
```

### 7.3 Default Transitions (Pipeline-Level)

```yaml
id: robust-pipeline
name: Pipeline with Global Error Handler
default_transitions:
  failed: global_error_handler
  timeout: global_error_handler

phases:
  - id: phase_a
    transitions:
      success: phase_b
    # failed/timeout → global_error_handler (from default_transitions)
    
  - id: phase_b
    transitions:
      success: phase_c
      failed: phase_b_recovery  # overrides default for 'failed'
    # timeout → global_error_handler (from default_transitions)
    
  - id: global_error_handler
    prompt_template: |
      An error occurred. Summarize what happened and suggest next steps.
```

### 7.4 Hybrid DAG + Transitions

```yaml
id: hybrid-pipeline
parallel: true

phases:
  # --- DAG section (parallel-capable) ---
  - id: research_a
    depends_on: []
    
  - id: research_b  
    depends_on: []
    
  - id: synthesize
    depends_on: [research_a, research_b]
    
  # --- Transition section (sequential state machine) ---
  - id: draft
    depends_on: [synthesize]  # bridge: depends on DAG phase
    transitions:
      success: review
      failed: draft_retry
      
  - id: review
    transitions:
      success: finalize
      failed: draft  # loop back
```

---

## 8. Edge Cases and Failure Modes

### 8.1 Transition Target Not Found

**Scenario:** `transitions.success: nonexistent_phase`  
**Handling:** Caught at template validation time (`validate_template` extended). Runtime also guards with `KeyError` → abort.

### 8.2 Outcome Not in Transitions Map

**Scenario:** Phase has `transitions: {success: next}` but phase fails.  
**Handling:** Check `default_transitions`. If no match there either → pipeline ends (not an error — unhandled outcome = terminal state). Log at WARNING level.

### 8.3 Phase Outputs Overwrite on Loop

**Scenario:** Phase `review` executes 3 times. What does `{review.output}` contain?  
**Handling:** Always the **latest** iteration. Previous iterations stored in `iteration_history` metadata. Prompt templates can access `{review.output}` (latest) — no way to access historical outputs in v1 (by design, to keep templates simple).

### 8.4 Retry + Transition Interaction

**Scenario:** Phase has `retries: 2` AND `transitions: {failed: error_handler}`.  
**Handling:** Retries execute **first** (all 3 attempts). Only after retries are exhausted does the phase report `failed`, which then triggers the transition. Retries are internal to a single phase execution; transitions are external routing between phases.

### 8.5 Transition to a DAG Phase

**Scenario:** Transition points to a phase that has `depends_on`.  
**Handling:** `depends_on` is **ignored** when a phase is reached via transition. The transition is an explicit "go here now" — dependency satisfaction was already handled by the DAG section. Log at DEBUG level.

### 8.6 Multiple Entry Points to Transition Chain

**Scenario:** Two DAG phases both have transitions that enter the same transition chain.  
**Handling:** Each DAG phase's transition chain runs independently and sequentially. The second chain sees `phase_outputs` from the first chain (they share state). This is safe but could be confusing — document clearly.

### 8.7 Transition Chain Never Terminates

**Scenario:** Every phase routes to another phase; no terminal state.  
**Handling:** `max_iterations` per-phase and `MAX_TOTAL_STEPS` global safety net. Both abort cleanly with descriptive error.

### 8.8 Callback Ordering with Loops

**Scenario:** `on_phase_complete` fires for phase `review` 3 times.  
**Handling:** Callbacks receive the phase_id and result as today. Callers (e.g., CLI progress display) must handle repeated phase_ids. Add `iteration` field to callback context.

### 8.9 File Writes on Loop Iterations

**Scenario:** Phase with `write_files: true` executes 3 times.  
**Handling:** Files are overwritten on each iteration (same working_dir, same FILE block paths). This is correct — latest iteration's output should be on disk.

---

## 9. Migration Path

### 9.1 Zero Breaking Changes

| Aspect | Before | After |
|--------|--------|-------|
| PhaseDefinition without transitions | Executes in DAG order | **Same** — no transitions = DAG behavior |
| PipelineTemplate without max_iterations | N/A | Gets default `max_iterations=10` (harmless — never triggers without transitions) |
| Existing YAML templates | Work as-is | **Same** — new fields are optional with safe defaults |
| PhaseSequencer | Executes pipelines | **Same class, untouched** — new behavior in `StateMachineSequencer` |

### 9.2 Template Version Bump

Templates using transitions should declare `version: "1.1.0"` or higher. The engine accepts any version — this is purely informational for template authors.

### 9.3 Gradual Adoption Path

1. **Phase 1:** Ship `transitions` field on PhaseDefinition (parsed but ignored by PhaseSequencer)
2. **Phase 2:** Ship `StateMachineSequencer` — opt-in via CLI flag or config
3. **Phase 3:** Make `StateMachineSequencer` the default (detects transitions automatically)
4. **Phase 4:** Add `default_transitions` to PipelineTemplate

### 9.4 CLI/Runner Integration

The `pipeline_runner.py` or `runner.py` currently instantiates `PhaseSequencer`. The change:

```python
# Before:
sequencer = PhaseSequencer(template, runner, config, ...)

# After:
has_transitions = any(p.transitions for p in template.phases)
if has_transitions:
    sequencer = StateMachineSequencer(template, runner, config, ...)
else:
    sequencer = PhaseSequencer(template, runner, config, ...)
```

Alternatively, `StateMachineSequencer` can detect "no transitions" and delegate entirely to `super().execute()`, making it a drop-in replacement.

---

## 10. Issue Breakdown

### Issue 1: Data Model — PhaseOutcome Enum + PhaseDefinition.transitions Field
**Size:** S  
**Depends on:** Nothing  
**Branch:** `feat/191-phase-transitions-data-model`

**Scope:**
- Add `PhaseOutcome` enum to `schemas.py`
- Add `transitions: Dict[str, str]` and `max_iterations: int` to `PhaseDefinition`
- Add `max_iterations: int` and `default_transitions: Dict[str, str]` to `PipelineTemplate`
- Update `TemplateEngine.load_template()` to parse new fields from YAML
- Add new fields to `known_fields` set in template loader (prevents "unknown fields dropped" warning)
- Update `__post_init__` normalization (None → empty dict, int clamping)

**Acceptance Criteria:**
- [ ] Existing templates load without changes (backward compat)
- [ ] New template with `transitions` field loads correctly
- [ ] `transitions: null` in YAML → empty dict
- [ ] `max_iterations: -1` → clamped to 0
- [ ] Unknown transition keys accepted (for future extensibility)
- [ ] Unit tests for all new field parsing and normalization

---

### Issue 2: Template Validation — Transition Graph Validation
**Size:** M  
**Depends on:** Issue 1  
**Branch:** `feat/191-transition-validation`

**Scope:**
- Extend `validate_template()`:
  - Validate transition target phase IDs exist
  - Validate transition keys against `PhaseOutcome` (warn on unknown, don't error)
  - Detect cycles in transition graph (warn if max_iterations set, error if not)
- Extend `validate_template_extended()`:
  - Check for unreachable phases (no depends_on AND no transition points to them AND not an entry point)
  - Warn on phases with both `depends_on` and incoming transitions (potential confusion)

**Acceptance Criteria:**
- [ ] `transitions: {success: nonexistent}` → validation error
- [ ] Cycle with `max_iterations > 0` → validation warning  
- [ ] Cycle without `max_iterations` but pipeline `max_iterations > 0` → warning
- [ ] Cycle without any max_iterations protection → validation error
- [ ] Unknown outcome key `transitions: {custom_outcome: next}` → warning (not error)
- [ ] Unit tests for all validation scenarios

---

### Issue 3: Outcome Determination — Map TaskResult to PhaseOutcome
**Size:** S  
**Depends on:** Issue 1  
**Branch:** `feat/191-outcome-determination`

**Scope:**
- Add `_determine_outcome(result, phase) → PhaseOutcome` method to `PhaseSequencer` (so it's available to both sequencers)
- Distinguish `timeout` from `failed` by checking error codes
- Add `TIMEOUT` and `EXEC_TIMEOUT` error codes to timeout detection
- Add `PhaseOutcome` to result metadata: `result["metadata"]["outcome"] = outcome.value`
- Update `_execute_wave_sequential` to annotate results with outcome (non-breaking addition)

**Acceptance Criteria:**
- [ ] Successful result → `PhaseOutcome.SUCCESS`
- [ ] Failed result → `PhaseOutcome.FAILED`
- [ ] Result with TIMEOUT error code → `PhaseOutcome.TIMEOUT`
- [ ] Skipped result → `PhaseOutcome.SKIPPED`
- [ ] Unknown state → `PhaseOutcome.FAILED` (safe default)
- [ ] Outcome stored in `result["metadata"]["outcome"]`
- [ ] Existing pipeline behavior unchanged (outcome annotation is additive)

---

### Issue 4: StateMachineSequencer — Core Execution Engine
**Size:** L (can split further if needed)  
**Depends on:** Issues 1, 2, 3  
**Branch:** `feat/191-state-machine-sequencer`

**Scope:**
- New class `StateMachineSequencer(PhaseSequencer)` in `sequencer.py`
- Override `execute()`:
  - Detect if template has transitions
  - If no transitions → delegate to `super().execute()`
  - If transitions → run DAG phases first, then state machine loop
- Implement state machine loop:
  - Phase execution (reuse `_execute_wave_sequential` for single-phase waves)
  - Outcome determination → transition routing
  - Iteration counting + `max_iterations` enforcement
  - Global step counter safety net
  - `iteration_history` tracking
- Handle `default_transitions` fallback
- Handle unmatched outcomes (terminal state)
- Proper callback invocation with iteration context

**Acceptance Criteria:**
- [ ] Pipeline with no transitions → identical behavior to PhaseSequencer
- [ ] Pipeline with linear transitions (A→B→C) executes correctly
- [ ] Pipeline with branching (A→B on success, A→C on failure) routes correctly
- [ ] Pipeline with loop (A→B→A) respects max_iterations
- [ ] max_iterations exceeded → clean abort with `MAX_ITERATIONS_EXCEEDED`
- [ ] `default_transitions` applied when phase has no matching transition
- [ ] Unmatched outcome → pipeline ends cleanly (not an error)
- [ ] `phase_outputs` updated correctly on each iteration
- [ ] `iteration_history` tracks all previous iterations
- [ ] `on_phase_start` / `on_phase_complete` callbacks fire for every iteration
- [ ] Integration test with a real template YAML

---

### Issue 5: Runner Integration + CLI
**Size:** S  
**Depends on:** Issue 4  
**Branch:** `feat/191-runner-integration`

**Scope:**
- Update `pipeline_runner.py` / `runner.py` to use `StateMachineSequencer` when transitions detected
- OR: make `StateMachineSequencer` the default (since it delegates to super() when no transitions)
- Update CLI output to show iteration counts for looping phases
- Update progress callbacks to handle repeated phase_ids gracefully

**Acceptance Criteria:**
- [ ] Existing CLI commands work unchanged with existing templates
- [ ] `orch run` with a transition template executes correctly
- [ ] Progress display shows iteration number for looping phases
- [ ] `--dry-run` works with transition templates

---

### Issue 6: Documentation + Example Templates
**Size:** S  
**Depends on:** Issue 5  
**Branch:** `feat/191-docs-examples`

**Scope:**
- Add example templates to `templates/` directory:
  - `review-loop.yaml` — write → review → revise loop
  - `error-recovery.yaml` — fallback chain on failure
  - `hybrid-dag-transitions.yaml` — mixed DAG + transitions
- Update README / docs with transition syntax reference
- Add CHANGELOG entry

**Acceptance Criteria:**
- [ ] All example templates pass `orch validate`
- [ ] Each example template has comments explaining the transitions
- [ ] Documentation covers all YAML fields and their defaults
- [ ] Migration guide for existing users

---

## 11. Risk Assessment

### High Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Infinite loops in production** | Pipeline runs forever, burns API credits | Runtime `max_iterations` + global step counter + static cycle detection at validation |
| **State corruption on loop iterations** | `phase_outputs` inconsistent, prompts use stale data | Overwrite semantics are clear; `_phase_outputs_lock` protects concurrent access |
| **Breaking existing pipelines** | All current users affected | `StateMachineSequencer.execute()` delegates to `super()` when no transitions; zero code path change for non-transition pipelines |

### Medium Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Retry + transition interaction confusion** | Users expect transition on first failure, but retries happen first | Document clearly: retries are internal, transitions are external. Consider `retry_before_transition: bool` field for v2 |
| **Complex template debugging** | Hard to trace why a pipeline took a certain path | Add execution trace log: `[TRANSITION] phase_a (success) → phase_b`. Include in pipeline result metadata |
| **Parallel wave + transition ambiguity** | Users put transitions on parallel phases, unexpected behavior | Validation warning when transition phase has siblings in same wave. Documentation. |
| **Memory growth on long loops** | `iteration_history` accumulates large results | Cap `iteration_history` entries (keep last N). Truncate stored partial outputs. |

### Low Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| **YAML syntax confusion** | Users misformat transitions | Validation catches structural errors. Example templates serve as reference. |
| **Performance overhead for non-transition pipelines** | Slightly slower | `if not has_transitions: return super().execute()` — zero overhead |
| **Callback contract change** | CLI/UI breaks on repeated phase_ids | Phase_id already passed to callbacks; iteration number added to context dict |

### What Needs Extra Testing

1. **Loop boundary conditions:** max_iterations = 1 (execute once, don't loop), max_iterations = 0 (use default), negative values
2. **Transition + retry interaction:** phase with retries=2, transitions={failed: handler} — verify all 3 attempts happen before transition
3. **DAG → transition bridge:** phase with both `depends_on` and `transitions` — verify DAG dependencies satisfied before transition chain starts
4. **Parallel wave containing a phase with transitions:** verify correct behavior (sequential execution, not parallel)
5. **Outcome determination edge cases:** executor raises exception (not TaskResult), task timeout vs phase timeout, empty result dict
6. **File writes in loops:** verify overwrite behavior, verify `files_written` metadata reflects latest iteration
7. **Pipeline callbacks in loops:** `on_pipeline_complete` fires once (not per iteration), `on_phase_complete` fires per iteration
8. **Large iteration counts:** 100+ iterations — verify memory, logging doesn't explode
9. **Concurrent pipeline runs:** two pipelines sharing a runner — verify state isolation (they already use separate `PhaseSequencer` instances, but confirm)
10. **Template hot-reload:** if templates are reloaded mid-run, transitions should use the version loaded at start

---

## Appendix: Execution Trace Example

For the review-loop template in Section 7.1:

```
[PIPELINE] Starting content-with-review
[DAG] No DAG-only phases, entering transition chain
[TRANSITION] Entry point: write_draft
[PHASE] write_draft (iteration 1/5) — executing
[PHASE] write_draft — SUCCESS
[TRANSITION] write_draft (success) → review
[PHASE] review (iteration 1/5) — executing  
[PHASE] review — FAILED (needs revision)
[TRANSITION] review (failed) → revise
[PHASE] revise (iteration 1/3) — executing
[PHASE] revise — SUCCESS
[TRANSITION] revise (success) → review
[PHASE] review (iteration 2/5) — executing
[PHASE] review — SUCCESS (approved)
[TRANSITION] review (success) → publish
[PHASE] publish (iteration 1/5) — executing
[PHASE] publish — SUCCESS
[TRANSITION] publish — no transitions defined, pipeline complete
[PIPELINE] Completed content-with-review (6 phase executions, 1 loop)
```
