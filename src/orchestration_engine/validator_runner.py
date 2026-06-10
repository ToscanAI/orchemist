"""validator_runner.py — Standalone subprocess entry point for external validation.

This module is the validator subprocess. It:
1. Reads JSON-RPC 2.0 requests from stdin (line-delimited).
2. Executes the requested operation (health check or test validation).
3. Writes JSON-RPC 2.0 responses to stdout.

Isolation requirements:
- MUST NOT import from validator.py, sequencer.py, daemon.py, or errors.py.
- Imports ONLY from ipc.py, test_store.py, and file_guard.py (plus stdlib).
  (file_guard.py is a stdlib-only leaf utility — safe to import.)

Invoked as:
    python3 -m orchestration_engine.validator_runner

Protocol:
    health  → HealthResult(status="ok")
    validate → ValidationResult(verdict, pass_rate, ...)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

# Only import from ipc, test_store, and file_guard — never from validator, sequencer, daemon, or errors  # noqa: E501
from orchestration_engine.file_guard import compute_hash
from orchestration_engine.ipc import (
    HealthRequest,
    HealthResult,
    IPCError,
    IPCProtocolError,
    TestDetail,
    ValidationRequest,
    ValidationResult,
    deserialize_request,
    serialize_error_response,
    serialize_response,
)
from orchestration_engine.pytest_output_parser import count_pytest_results
from orchestration_engine.test_store import TestStore, TestStoreError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON-RPC error codes
# ---------------------------------------------------------------------------

_ERR_INTERNAL = -32603
_ERR_INVALID_PARAMS = -32602


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _run_pytest_on_file(
    test_file: Path,
    repo_path: str,
    timeout_seconds: int,
) -> ValidationResult:
    """Run pytest on *test_file* and return a ValidationResult.

    Args:
        test_file:       Path to the test file to run.
        repo_path:       Working directory / sys.path root for pytest.
        timeout_seconds: Maximum seconds to wait before killing pytest.

    Returns:
        A ``ValidationResult`` with verdict, counts, and per-test details.
        Never raises — all errors are encoded in the result.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(test_file),
        "-v",
        "--tb=short",
        "--no-header",
    ]

    import time as _time  # noqa: PLC0415

    start = _time.monotonic()

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            cwd=repo_path if Path(repo_path).exists() else None,
            env=os.environ.copy(),
        )
        duration = _time.monotonic() - start
        return _parse_pytest_output(proc.stdout or "", proc.returncode, duration)

    except subprocess.TimeoutExpired:
        duration = _time.monotonic() - start
        return ValidationResult(
            verdict="ERROR",
            tests_total=0,
            pass_rate=0.0,
            details=[],
            tests_passed=0,
            tests_failed=0,
            tests_errored=0,
            duration_seconds=duration,
            retry_recommended=False,
            retry_reason=f"pytest timed out after {timeout_seconds}s",
        )

    except Exception as exc:  # noqa: BLE001
        return ValidationResult(
            verdict="ERROR",
            tests_total=0,
            pass_rate=0.0,
            details=[],
            tests_passed=0,
            tests_failed=0,
            tests_errored=0,
            duration_seconds=0.0,
            retry_recommended=False,
            retry_reason=f"unexpected error running pytest: {exc}",
        )


def _parse_pytest_output(
    stdout: str,
    returncode: int,
    duration: float,
) -> ValidationResult:
    """Parse pytest -v --tb=short output into a ValidationResult."""
    counts = count_pytest_results(stdout, returncode)
    passed = counts.passed
    failed = counts.failed
    errors = counts.errors
    total = counts.total

    # validator_runner-specific rule (NOT in the shared helper): if no tests were
    # collected and pytest returned non-zero, treat it as a single error so the
    # verdict downgrades to ERROR rather than a misleading PASS.
    if total == 0 and returncode != 0:
        errors = 1
        total = 1

    pass_rate = passed / total if total > 0 else 0.0

    # Determine verdict
    if errors > 0 and passed == 0 and failed == 0:
        verdict = "ERROR"
        retry_recommended = False
        retry_reason = ""
    elif failed > 0 or (errors > 0):
        verdict = "FAIL"
        retry_recommended = True
        retry_reason = f"{failed} test(s) failed, {errors} error(s)"
    else:
        verdict = "PASS"
        retry_recommended = False
        retry_reason = ""

    # Extract per-test details from verbose output
    details = _extract_test_details(stdout)

    return ValidationResult(
        verdict=verdict,
        tests_total=total,
        tests_passed=passed,
        tests_failed=failed,
        tests_errored=errors,
        pass_rate=pass_rate,
        duration_seconds=duration,
        details=details,
        retry_recommended=retry_recommended,
        retry_reason=retry_reason,
    )


def _extract_test_details(stdout: str) -> list[TestDetail]:
    """Extract per-test outcomes from pytest -v output."""
    import re  # noqa: PLC0415

    details: list[TestDetail] = []
    # Match lines like: "test_foo.py::test_bar PASSED" or "test_foo.py::test_baz FAILED"
    line_re = re.compile(
        r"^(.*::[\w\[\]\-]+)\s+(PASSED|FAILED|ERROR)\s*$",
        re.MULTILINE,
    )
    failure_blocks: dict[str, str] = {}

    # Extract failure messages from FAILED blocks
    lines = stdout.splitlines()
    current_test: str | None = None
    current_block: list[str] = []

    for line in lines:
        if line.startswith("FAILED ") or line.startswith("_ FAILED "):
            if current_test and current_block:
                failure_blocks[current_test] = "\n".join(current_block)
            current_test = line.split(" ")[1] if " " in line else line
            current_block = [line]
        elif current_test and (line.startswith("=") or line.startswith("PASSED ")):
            failure_blocks[current_test] = "\n".join(current_block)
            current_test = None
            current_block = []
        elif current_test:
            current_block.append(line)

    for match in line_re.finditer(stdout):
        test_name = match.group(1).strip()
        outcome_str = match.group(2)
        message = failure_blocks.get(test_name, "")
        details.append(
            TestDetail(
                test_name=test_name,
                outcome=outcome_str,
                message=message,
            )
        )

    return details


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------


def _handle_health(request: HealthRequest) -> HealthResult:  # noqa: ARG001
    """Handle a health ping request."""
    return HealthResult(status="ok")


def _handle_validate(request: ValidationRequest) -> ValidationResult:
    """Handle a validate request.

    This uses the test_store_path from the request params to locate the sealed
    test file, verifies its hash, and runs pytest.
    """
    # Parameters come from the JSON-RPC params dict
    run_id: str = request.run_id
    test_store_path: str = request.test_store_path
    repo_path: str = request.repo_path
    timeout_seconds: int = request.timeout_seconds
    expected_hash: str | None = request.test_manifest_hash  # used for integrity check

    # Verify repo exists
    if not Path(repo_path).exists():
        return ValidationResult(
            verdict="ERROR",
            tests_total=0,
            pass_rate=0.0,
            details=[],
            tests_passed=0,
            tests_failed=0,
            tests_errored=0,
            duration_seconds=0.0,
            retry_recommended=False,
            retry_reason=f"repo path does not exist: {repo_path}",
        )

    # Locate sealed test file
    store = TestStore(store_root=Path(test_store_path))
    try:
        test_file_path = store.get_test_path(run_id)
    except TestStoreError as exc:
        return ValidationResult(
            verdict="ERROR",
            tests_total=0,
            pass_rate=0.0,
            details=[],
            tests_passed=0,
            tests_failed=0,
            tests_errored=0,
            duration_seconds=0.0,
            retry_recommended=False,
            retry_reason=f"test store error: {exc}",
        )

    # Verify file is readable
    try:
        test_file_path.read_bytes()
    except PermissionError as exc:
        msg = f"permission denied reading test file: {exc}"
        return ValidationResult(
            verdict="ERROR",
            tests_total=0,
            pass_rate=0.0,
            details=[
                TestDetail(
                    test_name="read_test_file",
                    outcome="ERROR",
                    message=msg,
                )
            ],
            tests_passed=0,
            tests_failed=0,
            tests_errored=0,
            duration_seconds=0.0,
            retry_recommended=False,
            retry_reason=msg,
        )
    except OSError as exc:
        msg = f"error reading test file: {exc}"
        return ValidationResult(
            verdict="ERROR",
            tests_total=0,
            pass_rate=0.0,
            details=[
                TestDetail(
                    test_name="read_test_file",
                    outcome="ERROR",
                    message=msg,
                )
            ],
            tests_passed=0,
            tests_failed=0,
            tests_errored=0,
            duration_seconds=0.0,
            retry_recommended=False,
            retry_reason=msg,
        )

    # Verify hash integrity if expected_hash is provided
    if expected_hash:
        actual_hash = compute_hash(test_file_path)
        if actual_hash != expected_hash:
            msg = (
                f"test store integrity check failed: "
                f"expected hash {expected_hash[:16]}... "
                f"but got {actual_hash[:16]}..."
            )
            return ValidationResult(
                verdict="ERROR",
                tests_total=0,
                pass_rate=0.0,
                details=[
                    TestDetail(
                        test_name="integrity_check",
                        outcome="ERROR",
                        message=msg,
                    )
                ],
                tests_passed=0,
                tests_failed=0,
                tests_errored=0,
                duration_seconds=0.0,
                retry_recommended=False,
                retry_reason=msg,
            )

    # Run pytest
    return _run_pytest_on_file(test_file_path, repo_path, timeout_seconds)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _run_loop(stdin=None, stdout=None) -> None:
    """Read JSON-RPC requests from stdin, write responses to stdout.

    Args:
        stdin:  Input stream (defaults to sys.stdin.buffer).
        stdout: Output stream (defaults to sys.stdout.buffer).
    """
    if stdin is None:
        stdin = sys.stdin.buffer
    if stdout is None:
        stdout = sys.stdout.buffer

    while True:
        try:
            raw = stdin.readline()
        except (OSError, EOFError):
            break

        if not raw:
            # EOF — orchestrator closed stdin
            break

        line = raw.decode("utf-8", errors="replace")

        try:
            request = deserialize_request(line)
        except IPCProtocolError as exc:
            error_response = serialize_error_response(
                IPCError(
                    code=_ERR_INTERNAL,
                    message=f"protocol error: {exc}",
                ),
                request_id=0,
            )
            stdout.write(error_response.encode("utf-8"))
            stdout.flush()
            continue

        try:
            if isinstance(request, HealthRequest):
                result = _handle_health(request)
            elif isinstance(request, ValidationRequest):
                result = _handle_validate(request)
            else:
                raise IPCProtocolError(f"unknown request type: {type(request)}")

            response = serialize_response(result, request.id)

        except Exception as exc:  # noqa: BLE001
            response = serialize_error_response(
                IPCError(
                    code=_ERR_INTERNAL,
                    message=f"internal error: {exc}",
                ),
                request_id=request.id,
            )

        stdout.write(response.encode("utf-8"))
        stdout.flush()


if __name__ == "__main__":
    _run_loop()
