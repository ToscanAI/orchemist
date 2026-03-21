---
name: Bug Report
about: Something is broken — fix it through the pipeline
labels: bug, pipeline-ready
---

## Problem
<!-- What's broken? Include error messages, logs, reproduction steps -->
<!-- IMPORTANT: Describe the behavior fully in this issue. Do NOT rely on other issue numbers for context. -->
<!-- The pipeline agent reads ONLY this issue body — it cannot look up #123 or other references. -->
<!-- Related issues can be linked for tracking, but all behavioral context must be self-contained here. -->
<!-- FORMATTING: Avoid bare #N for ordinals (e.g. "attempt #1"). GitHub auto-links #N as issue references. Use "round 1", "attempt 1", or escape with backticks `#1`. -->

## Evidence
<!-- Concrete proof: log output, run IDs, screenshots -->

## Root Cause (if known)
<!-- Why is this happening? Which file/function? -->

## Behavioral Contracts
<!-- Explicit input→output contracts. Each must be testable in isolation. -->
<!-- Every contract must specify: Given [input/state], the system [observable outcome including terminal status] -->

### Core behavior
- Given [input/state], the system [observable outcome]

### Edge cases
- Given [boundary condition], the system [explicit outcome including terminal status]
- Given [error condition], the system [explicit outcome + logging behavior]

## Files likely affected
- `src/...`
- `tests/...`

## Acceptance Criteria
<!-- One checkbox per behavioral contract above -->
- [ ] ...
