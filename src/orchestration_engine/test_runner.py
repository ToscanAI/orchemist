"""test_runner.py — engine-executed pytest runner for acceptance_run phase.

Provides a pure, testable interface for running pytest and parsing its output.
No LLM involved. All I/O is injectable/mockable for unit testing.

Note: This module is used exclusively by the ``acceptance_run`` phase of the
OpenClaw coding pipeline, executed via ``OpenClawExecutor``. It is NOT
compatible with standalone (``AnthropicExecutor``) mode — the coding pipeline
must be run via ``PipelineRunner.openclaw()``.

Public API:
  - TestRunResult: dataclass with structured pytest result fields
  - run_pytest(test_file, timeout_seconds, extra_args): invoke subprocess pytest
  - parse_pytest_output(stdout, returncode): parse pytest -v --tb=short output
  - write_acceptance_results(result, output_dir, phase): write acceptance_results.json
  - format_failure_summary(result): human-readable failure summary for feedback context
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns for parsing pytest -v --tb=short output
# ---------------------------------------------------------------------------

# Summary line patterns (various pytest output formats):
# "2 passed, 1 failed, 0 errors in 0.35s"
# "3 passed in 0.12s"
# "1 error in 0.05s"
_SUMMARY_RE = re.compile(
    r"=+\s*"
    r"(?:(\d+)\s+passed)?"
    r"(?:,?\s*(\d+)\s+failed)?"
    r"(?:,?\s*(\d+)\s+error(?:s)?)?"
    r"\s+in\s+[\d.]+s"
    r"\s*=+",
)

# Fallback for errors-only lines: "3 errors in 0.05s"
_ERRORS_ONLY_RE = re.compile(
    r"=+\s*(\d+)\s+error(?:s)?\s+in\s+[\d.]+s\s*=+",
)

# "no tests ran" or "no tests collected"
_NO_TESTS_RE = re.compile(r"no tests (?:ran|collected)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TestRunResult:
    """Structured result of a pytest execution.

    Attributes:
        passed:          Number of tests that passed.
        failed:          Number of tests that failed assertions.
        errors:          Number of tests with errors (setup/teardown/import).
        total:           passed + failed + errors.
        pass_rate:       passed / total if total > 0 else 0.0.
        failure_details: Captured FAILED section text from --tb=short output.
        full_output:     Complete stdout+stderr from pytest.
        exit_code:       Subprocess return code (0 = all pass, non-zero = failures).
    """

    passed: int
    failed: int
    errors: int
    total: int
    pass_rate: float
    failure_details: str
    full_output: str
    exit_code: int


# ---------------------------------------------------------------------------
# Core parsing function
# ---------------------------------------------------------------------------

def parse_pytest_output(stdout: str, returncode: int) -> TestRunResult:
    """Parse pytest ``-v --tb=short`` stdout into a ``TestRunResult``.

    Handles all common output patterns:
    - Full summary: "X passed, Y failed, Z errors in N.Ns"
    - Partial: "X passed in N.Ns" (no failures)
    - Errors only: "Z errors in N.Ns" (import/setup errors)
    - No tests: "no tests ran" / "no tests collected"
    - Empty output

    Args:
        stdout:     Captured stdout+stderr from pytest.
        returncode: Process exit code.

    Returns:
        Populated ``TestRunResult``. Never raises.
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

    failure_details = _extract_failure_details(stdout or "")

    return TestRunResult(
        passed=passed,
        failed=failed,
        errors=errors,
        total=total,
        pass_rate=pass_rate,
        failure_details=failure_details,
        full_output=stdout or "",
        exit_code=returncode,
    )


def _extract_failure_details(stdout: str) -> str:
    """Extract FAILED/ERROR sections from pytest --tb=short output.

    Captures lines that start with FAILED or are part of a short traceback
    block following a FAILED line.

    Args:
        stdout: Full pytest stdout.

    Returns:
        Multi-line string of failure details, or empty string if none.
    """
    if not stdout:
        return ""

    lines = stdout.splitlines()
    details: List[str] = []
    in_failure_block = False

    for line in lines:
        stripped = line.strip()
        # Start capturing at FAILED or ERROR lines
        if stripped.startswith("FAILED ") or stripped.startswith("ERROR "):
            in_failure_block = True
            details.append(line)
        elif in_failure_block:
            # Stop at next test result marker or separator
            if (
                stripped.startswith("PASSED ")
                or stripped.startswith("===")
                or stripped.startswith("---")
            ):
                in_failure_block = False
                if stripped.startswith("==="):
                    # Don't include summary line in details
                    continue
            else:
                details.append(line)

    return "\n".join(details)


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

def run_pytest(
    test_file: str,
    timeout_seconds: int = 300,
    extra_args: Optional[List[str]] = None,
) -> TestRunResult:
    """Invoke pytest on ``test_file`` and return a ``TestRunResult``.

    Security:
    - Uses ``shell=False`` (list-form subprocess) — no shell injection.
    - Inherits ``os.environ`` for PATH.
    - Output is captured in memory (not written to disk by this function).

    Args:
        test_file:        Absolute path to the pytest test file.
        timeout_seconds:  Maximum seconds to wait for pytest (default 300).
        extra_args:       Optional additional pytest arguments.

    Returns:
        ``TestRunResult`` — never raises; errors are encoded in the result.
    """
    cmd = ["python3", "-m", "pytest", test_file, "-v", "--tb=short"]
    if extra_args:
        cmd.extend(extra_args)

    logger.info("test_runner: running %s (timeout=%ds)", " ".join(cmd), timeout_seconds)

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            env=os.environ.copy(),
        )
        combined = proc.stdout or ""
        logger.debug("test_runner: exit_code=%d output_len=%d", proc.returncode, len(combined))
        return parse_pytest_output(combined, proc.returncode)

    except subprocess.TimeoutExpired as exc:
        msg = f"pytest timed out after {exc.timeout}s"
        logger.warning("test_runner: %s", msg)
        return TestRunResult(
            passed=0, failed=0, errors=1, total=1,
            pass_rate=0.0,
            failure_details=msg,
            full_output=f"TIMEOUT: {msg}",
            exit_code=1,
        )

    except FileNotFoundError as exc:
        msg = f"python3 not found: {exc}"
        logger.error("test_runner: %s", msg)
        return TestRunResult(
            passed=0, failed=0, errors=1, total=1,
            pass_rate=0.0,
            failure_details=msg,
            full_output=f"ERROR: {msg}",
            exit_code=1,
        )

    except Exception as exc:  # noqa: BLE001
        msg = f"unexpected error running pytest: {exc}"
        logger.error("test_runner: %s", msg)
        return TestRunResult(
            passed=0, failed=0, errors=1, total=1,
            pass_rate=0.0,
            failure_details=msg,
            full_output=f"ERROR: {msg}",
            exit_code=1,
        )


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------

def write_acceptance_results(
    result: TestRunResult,
    output_dir: str,
    phase: str = "acceptance_run",
) -> None:
    """Write ``acceptance_results.json`` to ``output_dir``.

    Overwrites any previous file (including the placeholder written by the
    ``acceptance_test`` phase agent).

    Schema written:
    ::

        {
          "phase":           "acceptance_run",
          "status":          "pass" | "fail",
          "test_file":       "<output_dir>/acceptance_tests.py",
          "passed":          N,
          "failed":          M,
          "errors":          E,
          "total":           N+M+E,
          "pass_rate":       float,
          "failure_details": "...",
          "exit_code":       int
        }

    Args:
        result:     The ``TestRunResult`` from ``run_pytest()``.
        output_dir: Directory to write ``acceptance_results.json`` into.
        phase:      Value for the ``phase`` field (default ``"acceptance_run"``).
    """
    status = "pass" if (result.failed == 0 and result.errors == 0) else "fail"
    test_file = str(Path(output_dir) / "acceptance_tests.py")

    data = {
        "phase": phase,
        "status": status,
        "test_file": test_file,
        "passed": result.passed,
        "failed": result.failed,
        "errors": result.errors,
        "total": result.total,
        "pass_rate": result.pass_rate,
        "failure_details": result.failure_details,
        "exit_code": result.exit_code,
    }

    out_path = Path(output_dir) / "acceptance_results.json"
    os.makedirs(output_dir, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

    logger.info(
        "test_runner: wrote %s (status=%s, passed=%d, failed=%d, errors=%d)",
        out_path, status, result.passed, result.failed, result.errors,
    )


# ---------------------------------------------------------------------------
# Feedback formatting
# ---------------------------------------------------------------------------

def format_failure_summary(result: TestRunResult) -> str:
    """Build a human-readable markdown failure summary for feedback context.

    The returned string is safe for embedding in ``str.format()`` and
    f-string calls: all curly braces in failure details are escaped so
    they won't be interpreted as format placeholders.

    Args:
        result: The ``TestRunResult`` to summarise.

    Returns:
        A non-empty markdown string summarising test results.
    """
    if result.total == 0:
        return (
            "## Acceptance Test Results\n\n"
            "No tests were collected. Check that `acceptance_tests.py` exists "
            "and is importable.\n"
        )

    status_icon = "✅" if result.failed == 0 and result.errors == 0 else "❌"
    summary_line = (
        f"{status_icon} **{result.passed}/{result.total} tests passed** "
        f"(pass rate: {result.pass_rate:.1%})"
    )

    lines = [
        "## Acceptance Test Results\n",
        summary_line,
        "",
        f"- Passed: {result.passed}",
        f"- Failed: {result.failed}",
        f"- Errors: {result.errors}",
        f"- Total:  {result.total}",
    ]

    if result.failure_details:
        # Escape all { and } in the failure details so this summary is safe
        # for use inside str.format() / f-string template injection.
        safe_details = result.failure_details.replace("{", "{{").replace("}", "}}")
        lines += [
            "",
            "### Failure Details",
            "",
            "```",
            safe_details,
            "```",
        ]

    return "\n".join(lines)
