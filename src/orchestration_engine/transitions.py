"""Phase transition types for state-machine pipeline execution."""
from enum import Enum
from typing import Any, Dict, Optional


class PhaseOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


def determine_outcome(result: Dict[str, Any]) -> PhaseOutcome:
    """Map a TaskResult dict (from TaskResult.model_dump()) to a PhaseOutcome.

    Mapping rules:
      - ``TaskState.SUCCESS``            → ``PhaseOutcome.SUCCESS``
      - ``TaskState.FAILED`` with a      → ``PhaseOutcome.TIMEOUT``
        timeout error (error code
        ``"timeout"`` in the errors list)
      - ``TaskState.FAILED``             → ``PhaseOutcome.FAILED``
      - ``TaskState.PERMANENTLY_FAILED`` → ``PhaseOutcome.FAILED``
      - ``TaskState.RETRY``              → ``PhaseOutcome.FAILED``
      - ``TaskState.QUEUED``             → ``PhaseOutcome.FAILED``
        (unexpected terminal state — safe failure)
      - ``TaskState.RUNNING``            → ``PhaseOutcome.FAILED``
        (incomplete execution — safe failure)
      - ``TaskState.CANCELLED``          → ``PhaseOutcome.SKIPPED``
      - Any unknown / missing state      → ``PhaseOutcome.FAILED``
        (default safe failure)

    Args:
        result: A TaskResult serialised as a plain ``dict`` (e.g. via
            ``TaskResult.model_dump()``), or any result dict produced by
            the sequencer / executors.  The function only inspects the
            ``"state"`` and ``"errors"`` keys; all other keys are ignored.

    Returns:
        A :class:`PhaseOutcome` value that represents the logical outcome
        of the phase for routing and sequencing decisions.

    Examples:
        >>> determine_outcome({"state": "success", "result": {}})
        <PhaseOutcome.SUCCESS: 'success'>

        >>> determine_outcome({"state": "failed", "errors": [{"code": "timeout"}]})
        <PhaseOutcome.TIMEOUT: 'timeout'>

        >>> determine_outcome({"state": "cancelled"})
        <PhaseOutcome.SKIPPED: 'skipped'>

        >>> determine_outcome({})
        <PhaseOutcome.FAILED: 'failed'>
    """
    state = result.get("state", "")

    # Normalise: strip whitespace and lower-case in case values were stored
    # as raw strings with inconsistent casing.
    # Non-string state values (e.g. int, None from bad serialisation) skip
    # normalisation and fall through to the default FAILED return below.
    if isinstance(state, str):
        state = state.strip().lower()

    if state == "success":
        return PhaseOutcome.SUCCESS

    if state == "cancelled":
        return PhaseOutcome.SKIPPED

    if state == "failed":
        # Distinguish a timeout-triggered failure from a plain failure.
        # Executors signal a timeout by including a TaskError with
        # code="timeout" in the errors list (see openclaw_executor.py).
        errors = result.get("errors", []) or []
        for error in errors:
            # errors may be dicts (model_dump output) or TaskError objects
            if isinstance(error, dict):
                code = error.get("code", "")
            else:
                code = getattr(error, "code", "")
            if isinstance(code, str) and code.strip().lower() == "timeout":
                return PhaseOutcome.TIMEOUT
        return PhaseOutcome.FAILED

    # permanently_failed, retry, queued, running, and any unknown state all
    # resolve to FAILED as a safe default.
    return PhaseOutcome.FAILED


# ---------------------------------------------------------------------------
# Content-based verdict extraction (Issue #301)
# ---------------------------------------------------------------------------

_VERDICT_KEYWORDS = ("APPROVE", "REQUEST_CHANGES", "ABORT")
# Priority ordering: lower index = higher priority (Issue #600)
_VERDICT_PRIORITY_ORDER = ("REQUEST_CHANGES", "ABORT", "APPROVE")


def extract_verdict(text: str) -> Optional[str]:
    """Extract the highest-priority verdict keyword from *text*.

    Scans ALL lines of the input, collecting every verdict keyword found
    at the start of a line (case-insensitive). After scanning, applies
    priority ordering: REQUEST_CHANGES > ABORT > APPROVE.

    This ensures that reasoning prose such as "APPROVE would be premature"
    on one line does not shadow an actual REQUEST_CHANGES verdict on a
    later line.

    Args:
        text: Phase output text (may be multi-line).

    Returns:
        Lowercase keyword string (e.g. ``"approve"``, ``"request_changes"``,
        ``"abort"``), or ``None`` if no verdict keyword is found.

    Examples:
        >>> extract_verdict("APPROVE would be premature\\nREQUEST_CHANGES\\n...")
        'request_changes'

        >>> extract_verdict("APPROVE\\nCode looks good")
        'approve'

        >>> extract_verdict("")
    """
    if not text:
        return None

    lines = text.splitlines()
    found: set = set()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        for keyword in _VERDICT_KEYWORDS:
            if upper.startswith(keyword):
                found.add(keyword)

    # Apply priority: REQUEST_CHANGES > ABORT > APPROVE
    for keyword in _VERDICT_PRIORITY_ORDER:
        if keyword in found:
            return keyword.lower()

    return None
