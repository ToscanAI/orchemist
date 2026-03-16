---
name: Feature Request
about: New capability for Orchemist to implement through the pipeline
labels: enhancement, pipeline-ready
---

## User Story
<!-- Who needs this and why? -->
As a [role], I want [capability] so that [value].

## Context
<!-- Why now? What does the system currently do (or not do)? -->
<!-- IMPORTANT: Describe the behavior fully in this issue. Do NOT rely on other issue numbers for context. -->
<!-- The pipeline agent reads ONLY this issue body — it cannot look up #123 or other references. -->
<!-- Related issues can be linked for tracking, but all behavioral context must be self-contained here. -->

## Behavioral Contracts
<!-- Explicit input→output contracts. Each must be testable in isolation. -->
<!-- Every contract must specify: Given [input/state], the system [observable outcome including terminal status] -->

### Happy path
- Given [initial state], when [action], the system [observable outcome]

### Configuration
- Given [config option X], the system [behavior changes to Y]
- Given [no config provided], the system defaults to [explicit default]

### Error handling
- Given [invalid input], the system [rejects with specific error/status]
- Given [dependency unavailable], the system [explicit fallback + status]

### Edge cases
- Given [boundary condition], the system [explicit outcome]

## Integration points
<!-- How does this connect to existing code? -->
- Reads from: `src/...`
- Extends: `src/...`
- New files: `src/...`

## Acceptance Criteria
<!-- One checkbox per behavioral contract above -->
- [ ] ...
