"""validator.py — Orchestrator-side manager for the external validator subprocess.

Provides:
- ``ValidatorError(OrchestratorError)`` — raised on lifecycle or protocol failures.
- ``ValidationRequest`` — describes a validation job (run_id, paths, hash, timeout).
- ``ExternalValidator`` — spawn/communicate/shutdown lifecycle manager.

The external validator runs as an isolated subprocess (``validator_runner.py``),
communicating via JSON-RPC 2.0 over stdin/stdout pipes. This module is the
orchestrator-side only — never imported by the subprocess.

Usage::

    from orchestration_engine.validator import ExternalValidator, ValidationRequest, ValidatorError
    from orchestration_engine.ipc import ValidationResult

    validator = ExternalValidator(test_store_path="/tmp/test_store")
    validator.spawn()
    try:
        request = ValidationRequest(
            run_id="abc123",
            repo_path="/path/to/repo",
            test_store_path="/tmp/test_store",
            test_file_hash="<sha256>",
        )
        result = validator.validate(request)
        print(result.verdict)  # "PASS" | "FAIL" | "ERROR"
    finally:
        validator.shutdown()
"""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Optional

from orchestration_engine.errors import OrchestratorError
from orchestration_engine.ipc import (
    HealthRequest,
    HealthResult,
    IPCError,
    IPCProtocolError,
    ValidationRequest as IPCValidationRequest,
    ValidationResult,
    deserialize_response,
    serialize_request,
)

__all__ = [
    "ValidatorError",
    "ValidationRequest",
    "ExternalValidator",
]

# ---------------------------------------------------------------------------
# Timeout constants
# ---------------------------------------------------------------------------

_HEALTH_CHECK_TIMEOUT_SECONDS: float = 5.0
_SHUTDOWN_WAIT_SECONDS: float = 5.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ValidatorError(OrchestratorError):
    """Raised when the external validator lifecycle or protocol fails.

    This is an orchestrator-side exception only — never imported by
    ``validator_runner.py``.

    Examples:
        - ``spawn()`` fails to start the subprocess
        - Health check times out during ``spawn()``
        - ``validate()`` is called before ``spawn()``
        - Response parsing fails (invalid JSON-RPC)
        - Validation times out
    """


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ValidationRequest:
    """Describes a validation job to execute in the external validator subprocess.

    This is the orchestrator-facing request type. It is converted internally
    to the IPC protocol format before being sent to the subprocess.

    Attributes:
        run_id:            Pipeline run identifier. Used to locate the sealed
                           test file in the test store.
        repo_path:         Absolute path to the repository under test. Pytest
                           runs with this as the working directory.
        test_store_path:   Absolute path to the test store root directory.
        test_file_hash:    Expected SHA-256 hex digest of the sealed test file.
                           The subprocess performs an integrity check against
                           this value before running tests.
        timeout_seconds:   Maximum seconds to wait for the validation to complete.
                           Defaults to 300 (5 minutes).
    """

    run_id: str
    repo_path: str
    test_store_path: str
    test_file_hash: str
    timeout_seconds: int = 300


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------


def _read_line_with_timeout(
    stream,
    timeout_seconds: float,
) -> tuple[bytes | None, bool]:
    """Read a line from *stream* with a timeout.

    Uses a daemon thread so the call returns within *timeout_seconds* even
    if the underlying readline() blocks indefinitely.

    Args:
        stream:          Readable binary stream (e.g. ``Popen.stdout``).
        timeout_seconds: Maximum seconds to wait.

    Returns:
        A ``(line, timed_out)`` tuple where:
          - ``line`` is the bytes read (may be ``b""`` on EOF), or ``None`` on timeout.
          - ``timed_out`` is ``True`` when the read did not complete in time.
    """
    result: list[bytes | None] = [None]
    exc_holder: list[Exception | None] = [None]

    def _reader() -> None:
        try:
            result[0] = stream.readline()
        except Exception as exc:  # noqa: BLE001
            exc_holder[0] = exc

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        # Did not finish within the timeout window
        return None, True

    if exc_holder[0] is not None:
        raise exc_holder[0]

    return result[0], False


def _send_request(process: subprocess.Popen, line: str) -> None:
    """Write a serialized JSON-RPC line to the subprocess stdin.

    Args:
        process: The running ``Popen`` instance.
        line:    Newline-terminated JSON string.

    Raises:
        ValidatorError: If writing to stdin fails.
    """
    try:
        process.stdin.write(line.encode("utf-8"))
        process.stdin.flush()
    except (OSError, BrokenPipeError) as exc:
        raise ValidatorError(f"failed to write to validator subprocess stdin: {exc}") from exc


def _receive_response(
    process: subprocess.Popen,
    timeout_seconds: float,
) -> tuple[ValidationResult | HealthResult | IPCError, bool]:
    """Read and parse one JSON-RPC response from the subprocess stdout.

    Args:
        process:         The running ``Popen`` instance.
        timeout_seconds: Maximum seconds to wait for a response line.

    Returns:
        ``(result, timed_out)`` where ``timed_out`` is ``True`` when no response
        arrived within the timeout.

    Raises:
        ValidatorError: On protocol errors (invalid JSON-RPC).
    """
    raw, timed_out = _read_line_with_timeout(process.stdout, timeout_seconds)
    if timed_out:
        return None, True  # type: ignore[return-value]

    if raw is None or raw == b"":
        raise ValidatorError("invalid response: subprocess closed stdout unexpectedly")

    try:
        line = raw.decode("utf-8", errors="replace")
        response = deserialize_response(line)
    except (IPCProtocolError, UnicodeDecodeError) as exc:
        raise ValidatorError(f"invalid response: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValidatorError(f"invalid response: {exc}") from exc

    return response, False


# ---------------------------------------------------------------------------
# ExternalValidator
# ---------------------------------------------------------------------------


class ExternalValidator:
    """Orchestrator-side lifecycle manager for the external validator subprocess.

    Spawns ``orchestration_engine.validator_runner`` as a subprocess and
    communicates with it via JSON-RPC 2.0 over stdin/stdout.

    Lifecycle::

        validator = ExternalValidator(test_store_path="/tmp/store")
        validator.spawn()   # start subprocess + health check
        result = validator.validate(request)
        validator.shutdown()

    Thread safety: This class is NOT thread-safe. Do not call ``validate()``
    concurrently from multiple threads on the same instance.
    """

    def __init__(self, test_store_path: str) -> None:
        """Initialise the validator manager.

        Args:
            test_store_path: Absolute path to the test store root directory.
                             Passed to the subprocess as the store root for
                             locating sealed test files.
        """
        self._test_store_path = test_store_path
        self._process: subprocess.Popen | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def spawn(self) -> None:
        """Start the validator subprocess and verify it is alive.

        Spawns ``sys.executable -m orchestration_engine.validator_runner`` and
        sends a health request. Waits up to 5 seconds for a ``HealthResult``
        response. If the health check does not return within 5 seconds, kills
        the subprocess and raises ``ValidatorError``.

        Raises:
            ValidatorError: With "failed to spawn" when the subprocess cannot
                be started (e.g. interpreter not found).
            ValidatorError: With "health check timeout" when the subprocess
                does not respond to the health ping within 5 seconds.
        """
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-m", "orchestration_engine.validator_runner"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            raise ValidatorError(f"failed to spawn validator subprocess: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ValidatorError(f"failed to spawn validator subprocess: {exc}") from exc

        # Send health check and wait for HealthResult
        health_request = HealthRequest()
        health_line = serialize_request(health_request)

        try:
            _send_request(self._process, health_line)
        except ValidatorError:
            self._kill_process()
            raise

        response, timed_out = _read_line_with_timeout(
            self._process.stdout, _HEALTH_CHECK_TIMEOUT_SECONDS
        )

        if timed_out:
            self._kill_process()
            raise ValidatorError("health check timeout: validator subprocess did not respond")

        if response is None or response == b"":
            self._kill_process()
            raise ValidatorError(
                "health check timeout: validator subprocess closed stdout before responding"
            )

        try:
            line = response.decode("utf-8", errors="replace")
            result = deserialize_response(line)
        except (IPCProtocolError, Exception) as exc:
            self._kill_process()
            raise ValidatorError(f"health check failed: invalid response: {exc}") from exc

        if not isinstance(result, HealthResult) or result.status != "ok":
            self._kill_process()
            raise ValidatorError(
                f"health check failed: unexpected response: {result}"
            )

    def validate(self, request: ValidationRequest) -> ValidationResult:
        """Run acceptance tests in the validator subprocess.

        Args:
            request: Describes the validation job — which run, which repo,
                     which test store, and the expected file hash.

        Returns:
            ``ValidationResult`` with verdict, counts, pass_rate, and details.

        Raises:
            ValidatorError: With "not spawned" when called before ``spawn()``.
            ValidatorError: With "timeout" when the subprocess does not respond
                within ``request.timeout_seconds``.
            ValidatorError: With "invalid response" when the response is not
                valid JSON-RPC.
        """
        if self._process is None:
            raise ValidatorError(
                "not spawned: call spawn() before validate()"
            )

        # Convert to IPC-level ValidationRequest
        ipc_request = IPCValidationRequest(
            run_id=request.run_id,
            test_store_path=request.test_store_path,
            repo_path=request.repo_path,
            branch="",  # not used by validator_runner; test store is located by run_id
            timeout_seconds=request.timeout_seconds,
            test_manifest_hash=request.test_file_hash,
        )

        line = serialize_request(ipc_request)

        _send_request(self._process, line)

        response, timed_out = _read_line_with_timeout(
            self._process.stdout, float(request.timeout_seconds)
        )

        if timed_out:
            # Kill the subprocess — it's unresponsive
            self._kill_process()
            raise ValidatorError(
                f"timeout: validator subprocess did not respond within "
                f"{request.timeout_seconds}s"
            )

        if response is None or response == b"":
            raise ValidatorError(
                "invalid response: subprocess closed stdout unexpectedly"
            )

        try:
            line = response.decode("utf-8", errors="replace")
            result = deserialize_response(line)
        except (IPCProtocolError, UnicodeDecodeError) as exc:
            raise ValidatorError(f"invalid response: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ValidatorError(f"invalid response: {exc}") from exc

        if isinstance(result, IPCError):
            # Subprocess returned a JSON-RPC error — translate to ValidationResult ERROR
            return ValidationResult(
                verdict="ERROR",
                tests_total=0,
                pass_rate=0.0,
                details=[],
                retry_recommended=False,
                retry_reason=f"validator subprocess error: {result.message}",
            )

        if not isinstance(result, ValidationResult):
            raise ValidatorError(
                f"invalid response: expected ValidationResult, got {type(result).__name__}"
            )

        return result

    def shutdown(self) -> None:
        """Gracefully stop the validator subprocess.

        Sends EOF to stdin (closes the pipe), waits up to 5 seconds for the
        process to exit, then calls ``process.kill()`` if it has not stopped.

        Idempotent — calling shutdown() on an already-exited subprocess
        completes without error.
        """
        if self._process is None:
            return

        # Close stdin to signal EOF — the subprocess's read loop exits on EOF
        try:
            if self._process.stdin and not self._process.stdin.closed:
                self._process.stdin.close()
        except OSError:
            pass

        # Wait up to _SHUTDOWN_WAIT_SECONDS for graceful exit
        try:
            self._process.wait(timeout=_SHUTDOWN_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            # Graceful exit did not happen — force-kill
            try:
                self._process.kill()
                self._process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass

        self._process = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _kill_process(self) -> None:
        """Kill the subprocess and clear the process handle."""
        if self._process is None:
            return
        try:
            self._process.kill()
            self._process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._process = None
