---
name: Bug Report
about: Something is broken — fix it through the pipeline
labels: bug, pipeline-ready
---

## Problem
<!-- What's broken? Include error messages, logs, reproduction steps -->

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
