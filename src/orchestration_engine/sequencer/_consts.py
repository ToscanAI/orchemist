"""Module-level constants for the phase sequencer (EPIC #942, sub-issue 953a).

These were extracted VERBATIM from the former single-file ``sequencer.py``.
They are pure data (no class behaviour) and are re-exported by the package
facade (:mod:`orchestration_engine.sequencer`) so the import surface is
byte-identical at every call site.
"""

# Module-level constant for output-length validation (Issue #351).
# Kept here so it is allocated once, not on every _validate_phase_output call.
_TERMINAL_PUNCTUATION: frozenset = frozenset(".!?:")

# Default supervisor prompt template (Issue #194).
# Placeholders: {rubric}, {phase_output}
_DEFAULT_SUPERVISOR_PROMPT = """\
You are a quality supervisor evaluating the output of a pipeline phase.

## RUBRIC
{rubric}

## OUTPUT
{phase_output}

## Instructions
Review the phase output against the rubric above.

Respond with exactly ONE of the following verdicts on the **first non-blank line**:

- `APPROVE: <brief reason>` — output meets all criteria, pipeline may continue
- `REVISE: <specific feedback>` — output needs improvement; describe exactly what to fix
- `ABORT: <reason>` — output is fundamentally flawed or dangerous; pipeline must stop

Your verdict line must start with APPROVE, REVISE, or ABORT.
"""
