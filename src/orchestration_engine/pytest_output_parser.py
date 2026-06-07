"""pytest_output_parser.py — shared pytest summary-line counting (Issue #927).

A single stdlib-only helper that extracts pass/fail/error counts from
``pytest -v --tb=short`` stdout. Both :func:`test_runner.parse_pytest_output`
and ``validator_runner._parse_pytest_output`` call :func:`count_pytest_results`
and wrap the counts into their own return types (``TestRunResult`` /
``ValidationResult`` respectively).

Isolation guarantee (load-bearing for ``validator_runner``):
this module imports ONLY ``re`` and ``typing.NamedTuple`` — no engine modules,
no pydantic, no yaml. Importing it triggers zero engine imports, so the
validator subprocess startup cost is unaffected.

The counting/regex below is character-identical to the two original
implementations (incl. ``re.IGNORECASE`` on the no-tests probe). The one rule
that does NOT live here — ``errors = 1`` when ``total == 0 and returncode != 0``
— is the validator wrapper's responsibility (see spec §2.4), because
``test_runner`` must NOT apply it (``test_empty_output`` asserts ``errors == 0``).
"""

from __future__ import annotations

import re
from typing import NamedTuple

__all__ = ["count_pytest_results", "_PytestCounts"]


# Summary line patterns (various pytest output formats):
# "2 passed, 1 failed, 0 errors in 0.35s"
# "3 passed in 0.12s"
# "1 error in 0.05s"
# Compiled once at module scope (not per call).
_SUMMARY_RE = re.compile(
    r"=+\s*"
    r"(?:(\d+)\s+passed)?"
    r"(?:,?\s*(\d+)\s+failed)?"
    r"(?:,?\s*(\d+)\s+error(?:s)?)?"
    r"\s+in\s+[\d.]+s"
    r"\s*=+",
)

# "no tests ran" or "no tests collected"
_NO_TESTS_RE = re.compile(r"no tests (?:ran|collected)", re.IGNORECASE)


class _PytestCounts(NamedTuple):
    """Parsed pytest counts. ``total == passed + failed + errors``."""

    passed: int
    failed: int
    errors: int
    total: int
    pass_rate: float


def count_pytest_results(stdout: str, returncode: int) -> _PytestCounts:
    """Extract pass/fail/error counts from ``pytest -v --tb=short`` output.

    Handles:
    - Full summary: ``"X passed, Y failed, Z error(s) in N.Ns"``
    - Partial: ``"X passed in N.Ns"``
    - No-tests: ``"no tests ran/collected"``
    - Empty output

    Does NOT apply the validator_runner ``total == 0 and returncode != 0 ->
    errors = 1`` rule — that is the caller's responsibility.

    Args:
        stdout:     Captured stdout (+stderr) from pytest.
        returncode: Process exit code. Accepted for signature parity with the
            wrappers; not consulted by the shared counting logic.

    Returns:
        ``_PytestCounts(passed, failed, errors, total, pass_rate)`` where
        ``total == passed + failed + errors`` and
        ``pass_rate == passed / total if total > 0 else 0.0``. Never raises.
    """
    passed = 0
    failed = 0
    errors = 0

    if stdout:
        # Primary: look for the === ... in N.Ns === summary line
        match = _SUMMARY_RE.search(stdout)
        if match:
            passed = int(match.group(1) or 0)
            failed = int(match.group(2) or 0)
            errors = int(match.group(3) or 0)
        elif _NO_TESTS_RE.search(stdout):
            # "no tests ran" — all zeros
            pass
        else:
            # Fallback: scan for individual count patterns when the combined
            # summary line doesn't match (e.g. unusual pytest output formats)
            m_passed = re.search(r"(\d+)\s+passed", stdout)
            m_failed = re.search(r"(\d+)\s+failed", stdout)
            m_errors = re.search(r"(\d+)\s+error(?:s)?", stdout)
            if m_passed:
                passed = int(m_passed.group(1))
            if m_failed:
                failed = int(m_failed.group(1))
            if m_errors:
                errors = int(m_errors.group(1))

    total = passed + failed + errors
    pass_rate = passed / total if total > 0 else 0.0
    return _PytestCounts(
        passed=passed,
        failed=failed,
        errors=errors,
        total=total,
        pass_rate=pass_rate,
    )
