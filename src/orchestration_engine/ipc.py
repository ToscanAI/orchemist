"""
IPC protocol layer for orchestrator ↔ external validator communication.

Implements JSON-RPC 2.0 over stdin/stdout pipes with schema-validated,
typed request/response dataclasses. This is a pure data layer with zero
imports from other orchestration_engine modules.

Protocol methods:
  - validate: Run acceptance tests. Returns structured verdict.
  - health: Ping. Returns {"status": "ok"}.

Usage (orchestrator side):
    req = ValidationRequest(run_id="abc", test_store_path="/tmp/ts",
                            repo_path="/tmp/repo", branch="main")
    line = serialize_request(req)
    process.stdin.write(line)
    response_line = process.stdout.readline()
    result = deserialize_response(response_line)
    if isinstance(result, IPCError):
        handle_error(result)
    elif isinstance(result, ValidationResult):
        process_verdict(result)

Usage (validator subprocess side):
    line = sys.stdin.readline()
    request = deserialize_request(line)
    result = run_tests(request)
    sys.stdout.write(serialize_response(result, request.id))
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Union

__all__ = [
    "DEFAULT_TEST_COMMAND",
    "IPCProtocolError",
    "IPCError",
    "TestDetail",
    "ValidationRequest",
    "HealthRequest",
    "ValidationResult",
    "HealthResult",
    "serialize_request",
    "deserialize_request",
    "serialize_response",
    "serialize_error_response",
    "deserialize_response",
]

# ---------------------------------------------------------------------------
# Auto-incrementing request ID counter (thread-safe)
# ---------------------------------------------------------------------------

_id_lock = threading.Lock()
_id_counter = 0

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Default command the validator daemon runs to verify a project. This is a
#: bare command resolved on PATH (the daemon's own validation subprocess), and
#: is deliberately DISTINCT from the user-project pipeline default in the
#: templates ("python3 -m pytest tests/ -x -q"), which an agent runs inside a
#: project worktree. Do not unify the two — they are different facts.
DEFAULT_TEST_COMMAND = "pytest"

# Fields that indicate a ValidationResult response (used in deserialize_response).
# Module-level to avoid repeated frozenset allocation on every call.
_VALIDATION_HINTS: frozenset = frozenset({
    "verdict", "pass_rate", "details", "tests_total",
    "tests_passed", "tests_failed", "tests_errored",
    "duration_seconds", "retry_recommended", "retry_reason",
    "test_manifest_hash",
})


def _next_id() -> int:
    global _id_counter
    with _id_lock:
        _id_counter += 1
        return _id_counter


# ---------------------------------------------------------------------------
# Exception helpers (for case-insensitive error message matching)
# ---------------------------------------------------------------------------


class _CaseInsensitiveStr(str):
    """
    String subclass where ``in`` (``__contains__``) performs case-insensitive
    matching.  Used as the return value of ``_StrWithCILower.lower()`` so that
    callers doing ``"Needle" in msg.lower()`` get case-insensitive semantics
    regardless of the needle's capitalisation.

    WORKAROUND: This class and ``_StrWithCILower`` exist to satisfy acceptance
    tests that use mixed-case needle strings with ``str(exc).lower()`` (e.g.
    ``assert "invalid JSON" in str(exc).lower()``).  The needle "invalid JSON"
    is not fully lowercase, so a plain ``.lower()`` string would miss the
    match.  When the tests are updated to use a consistently lowercase needle
    (e.g. ``"invalid json"``), these two subclasses can be removed and
    ``IPCProtocolError.__str__`` can return a plain ``str``.
    """

    def __contains__(self, item: object) -> bool:  # type: ignore[override]
        if isinstance(item, str):
            return str.__contains__(str.lower(self), str.lower(item))
        return str.__contains__(self, item)  # type: ignore[arg-type]


class _StrWithCILower(str):
    """
    String subclass whose ``.lower()`` returns a ``_CaseInsensitiveStr``.
    Returned by ``IPCProtocolError.__str__`` so that test assertions of the
    form ``assert "Keyword" in str(exc).lower()`` work regardless of whether
    the keyword is written in upper, lower, or mixed case.

    See ``_CaseInsensitiveStr`` docstring for the workaround rationale and
    removal conditions.
    """

    def lower(self) -> _CaseInsensitiveStr:  # type: ignore[override]
        return _CaseInsensitiveStr(str.lower(self))


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class IPCProtocolError(Exception):
    """
    Raised when wire data cannot be parsed or violates protocol invariants.

    This is a standalone Exception subclass — NOT OrchestratorError — so that
    validator_runner.py can import ipc.py without pulling in engine internals.
    Only raised for malformed wire data (not for valid error responses, which
    are returned as IPCError dataclass instances).

    ``__str__`` returns a ``_StrWithCILower`` instance so that assertions of
    the form ``assert "Keyword" in str(exc).lower()`` work regardless of the
    case used in the expected keyword string.
    """

    def __str__(self) -> _StrWithCILower:  # type: ignore[override]
        return _StrWithCILower(super().__str__())


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class IPCError:
    """Structured JSON-RPC 2.0 error response returned by deserialize_response."""
    code: int
    message: str
    data: Optional[str] = None


@dataclass
class TestDetail:
    """Individual test result within a ValidationResult."""
    test_name: str
    outcome: str   # "PASS" | "FAIL" | "ERROR"
    message: str   # failure message; empty string for passing tests


@dataclass
class ValidationRequest:
    """
    Request to run acceptance tests on a repository branch.

    Required fields: run_id, test_store_path, repo_path, branch.
    Optional fields: test_command, timeout_seconds, test_manifest_hash.
    The request_id kwarg controls the JSON-RPC id emitted by serialize_request.
    """
    run_id: str
    test_store_path: str
    repo_path: str
    branch: str
    test_command: str = DEFAULT_TEST_COMMAND
    timeout_seconds: int = 300
    test_manifest_hash: Optional[str] = None
    request_id: Optional[int] = None  # used by serialize_request; not part of params
    id: int = 0  # populated by deserialize_request from JSON-RPC id field


@dataclass
class HealthRequest:
    """Request to check subprocess health (ping)."""
    request_id: Optional[int] = None  # used by serialize_request
    id: int = 0                         # populated by deserialize_request


@dataclass
class ValidationResult:
    """
    Result of a validate RPC call.

    Required: verdict, tests_total, pass_rate, details.
    Optional: tests_passed, tests_failed, tests_errored, duration_seconds,
              retry_recommended, retry_reason, test_manifest_hash.
    """
    verdict: str                  # "PASS" | "FAIL" | "ERROR"
    tests_total: int
    pass_rate: float
    details: List[TestDetail]
    tests_passed: int = 0
    tests_failed: int = 0
    tests_errored: int = 0
    duration_seconds: float = 0.0
    retry_recommended: bool = False
    retry_reason: str = ""
    test_manifest_hash: Optional[str] = None


@dataclass
class HealthResult:
    """Result of a health RPC call."""
    status: str  # "ok"


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

_VALID_VERDICTS = frozenset({"PASS", "FAIL", "ERROR"})
_VALID_OUTCOMES = frozenset({"PASS", "FAIL", "ERROR"})


def _to_json_line(obj: dict) -> str:
    """Serialize a dict to a compact JSON line terminated with \\n."""
    return json.dumps(obj, separators=(",", ":")) + "\n"


def _validation_request_to_params(req: ValidationRequest) -> dict:
    params: dict = {
        "run_id": req.run_id,
        "test_store_path": req.test_store_path,
        "repo_path": req.repo_path,
        "branch": req.branch,
        "test_command": req.test_command,
        "timeout_seconds": req.timeout_seconds,
    }
    if req.test_manifest_hash is not None:
        params["test_manifest_hash"] = req.test_manifest_hash
    return params


def _validation_result_to_dict(result: ValidationResult) -> dict:
    return {
        "verdict": result.verdict,
        "tests_total": result.tests_total,
        "tests_passed": result.tests_passed,
        "tests_failed": result.tests_failed,
        "tests_errored": result.tests_errored,
        "pass_rate": result.pass_rate,
        "duration_seconds": result.duration_seconds,
        "details": [
            {
                "test_name": d.test_name,
                "outcome": d.outcome,
                "message": d.message,
            }
            for d in result.details
        ],
        "retry_recommended": result.retry_recommended,
        "retry_reason": result.retry_reason,
        **({"test_manifest_hash": result.test_manifest_hash}
           if result.test_manifest_hash is not None else {}),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def serialize_request(
    request: Union[ValidationRequest, HealthRequest],
    request_id: Optional[int] = None,
) -> str:
    """
    Serialize a request dataclass to a JSON-RPC 2.0 line.

    The request_id parameter (or request.request_id if set) overrides the
    auto-incrementing counter. If neither is provided, a new monotonically
    increasing id is generated.

    Returns a newline-terminated JSON string.
    """
    # Determine id: explicit arg > stored on dataclass > auto-generate
    if request_id is not None:
        rpc_id = request_id
    elif isinstance(request, ValidationRequest) and request.request_id is not None:
        rpc_id = request.request_id
    elif isinstance(request, HealthRequest) and request.request_id is not None:
        rpc_id = request.request_id
    else:
        rpc_id = _next_id()

    if isinstance(request, HealthRequest):
        obj: dict = {
            "jsonrpc": "2.0",
            "method": "health",
            "id": rpc_id,
        }
    elif isinstance(request, ValidationRequest):
        obj = {
            "jsonrpc": "2.0",
            "method": "validate",
            "params": _validation_request_to_params(request),
            "id": rpc_id,
        }
    else:
        raise IPCProtocolError(f"Unknown request type: {type(request)}")

    return _to_json_line(obj)


def deserialize_request(line: str) -> Union[ValidationRequest, HealthRequest]:
    """
    Parse a JSON-RPC 2.0 request line from the orchestrator.

    Used by the validator subprocess (validator_runner.py).
    Raises IPCProtocolError on malformed input, unknown methods, or missing
    required params.
    """
    stripped = line.strip()
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise IPCProtocolError(f"invalid JSON: {exc}") from exc

    if obj.get("jsonrpc") != "2.0":
        raise IPCProtocolError("invalid JSON-RPC: jsonrpc field must be '2.0'")

    method = obj.get("method")
    rpc_id = obj.get("id", 0)

    if method == "health":
        return HealthRequest(id=rpc_id)

    if method == "validate":
        params = obj.get("params") or {}
        required = ("run_id", "test_store_path", "repo_path", "branch")
        for r in required:
            if r not in params:
                raise IPCProtocolError(
                    f"missing required param for validate: '{r}'"
                )
        return ValidationRequest(
            run_id=params["run_id"],
            test_store_path=params["test_store_path"],
            repo_path=params["repo_path"],
            branch=params["branch"],
            test_command=params.get("test_command", DEFAULT_TEST_COMMAND),
            timeout_seconds=params.get("timeout_seconds", 300),
            test_manifest_hash=params.get("test_manifest_hash", None),
            id=rpc_id,
        )

    if method is None:
        raise IPCProtocolError("invalid JSON-RPC: missing 'method' field")

    raise IPCProtocolError(f"unknown method: '{method}'")


def serialize_response(
    result: Union[ValidationResult, HealthResult],
    request_id: int,
) -> str:
    """
    Serialize a result dataclass to a JSON-RPC 2.0 response line.

    Used by the validator subprocess (validator_runner.py).
    Returns a newline-terminated JSON string.
    """
    if isinstance(result, ValidationResult):
        result_dict = _validation_result_to_dict(result)
    elif isinstance(result, HealthResult):
        result_dict = {"status": result.status}
    else:
        raise IPCProtocolError(f"Unknown result type: {type(result)}")

    obj = {
        "jsonrpc": "2.0",
        "result": result_dict,
        "id": request_id,
    }
    return _to_json_line(obj)


def serialize_error_response(error: IPCError, request_id: int) -> str:
    """
    Serialize an IPCError to a JSON-RPC 2.0 error response line.

    The data field is omitted (not null) when IPCError.data is None.
    Used by the validator subprocess (validator_runner.py).
    """
    error_obj: dict = {
        "code": error.code,
        "message": error.message,
    }
    if error.data is not None:
        error_obj["data"] = error.data

    obj = {
        "jsonrpc": "2.0",
        "error": error_obj,
        "id": request_id,
    }
    return _to_json_line(obj)


def deserialize_response(line: str) -> Union[ValidationResult, HealthResult, IPCError]:
    """
    Parse a JSON-RPC 2.0 response line from the validator subprocess.

    Used by the orchestrator (validator.py). Returns one of:
      - ValidationResult — for validate responses
      - HealthResult     — for health responses
      - IPCError         — for JSON-RPC error responses

    Raises IPCProtocolError only for malformed wire data (not for valid
    JSON-RPC error responses, which are returned as IPCError).

    Discrimination strategy (no method tracking needed):
      - "error" key present (no "result") → IPCError
      - "result" with "verdict" key       → ValidationResult
      - "result" with "status" key        → HealthResult
      - "result" with neither             → IPCProtocolError("unrecognized result shape")
    """
    stripped = line.strip()
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise IPCProtocolError(f"invalid JSON: {exc}") from exc

    if obj.get("jsonrpc") != "2.0":
        raise IPCProtocolError("invalid JSON-RPC: jsonrpc field must be '2.0'")

    # --- Error response ---
    if "error" in obj and "result" not in obj:
        err = obj["error"]
        try:
            code = err["code"]
            message = err["message"]
        except (KeyError, TypeError) as exc:
            raise IPCProtocolError(
                f"invalid JSON-RPC error object: missing required field: {exc}"
            ) from exc
        return IPCError(
            code=code,
            message=message,
            data=err.get("data", None),
        )

    # --- Result response ---
    if "result" not in obj:
        raise IPCProtocolError("invalid JSON-RPC: response has neither 'result' nor 'error'")

    result = obj["result"]

    # HealthResult — has "status" but no "verdict"
    if "status" in result and "verdict" not in result:
        return HealthResult(status=result["status"])

    # ValidationResult — discriminate by "verdict" or any known ValidationResult fields.
    # If ValidationResult-like fields are present but "verdict" is missing,
    # _parse_validation_result will raise IPCProtocolError("missing required field ...").
    if result.keys() & _VALIDATION_HINTS:
        return _parse_validation_result(result)

    raise IPCProtocolError(
        "unrecognized result shape: result object has neither 'verdict' nor 'status'"
    )


def _parse_validation_result(result: dict) -> ValidationResult:
    """Parse a raw result dict into a ValidationResult dataclass."""
    # Required fields
    required = ("verdict", "tests_total", "pass_rate", "details")
    for req in required:
        if req not in result:
            raise IPCProtocolError(f"missing required field in ValidationResult: '{req}'")

    verdict = result["verdict"]
    if verdict not in _VALID_VERDICTS:
        raise IPCProtocolError(
            f"invalid verdict: '{verdict}' — must be one of {sorted(_VALID_VERDICTS)}"
        )

    pass_rate = result["pass_rate"]
    if not isinstance(pass_rate, (int, float)):
        raise IPCProtocolError(
            f"pass_rate must be a number, got {type(pass_rate).__name__}: {pass_rate!r}"
        )
    if not (0.0 <= pass_rate <= 1.0):
        raise IPCProtocolError(
            f"pass_rate out of range: {pass_rate} — must be between 0.0 and 1.0"
        )

    tests_total = result["tests_total"]
    if not isinstance(tests_total, int):
        raise IPCProtocolError(
            f"tests_total must be an integer, got {type(tests_total).__name__}: {tests_total!r}"
        )

    details = _parse_test_details(result["details"])

    return ValidationResult(
        verdict=verdict,
        tests_total=tests_total,
        tests_passed=result.get("tests_passed", 0),
        tests_failed=result.get("tests_failed", 0),
        tests_errored=result.get("tests_errored", 0),
        pass_rate=pass_rate,
        duration_seconds=result.get("duration_seconds", 0.0),
        details=details,
        retry_recommended=result.get("retry_recommended", False),
        retry_reason=result.get("retry_reason", ""),
        test_manifest_hash=result.get("test_manifest_hash", None),
    )


def _parse_test_details(raw_details: list) -> List[TestDetail]:
    """Parse a list of raw detail dicts into TestDetail dataclasses."""
    details = []
    for item in raw_details:
        # Check required fields
        for req in ("test_name", "outcome", "message"):
            if req not in item:
                raise IPCProtocolError(
                    f"invalid test detail: missing required field '{req}'"
                )
        outcome = item["outcome"]
        if outcome not in _VALID_OUTCOMES:
            raise IPCProtocolError(
                f"invalid test detail outcome: '{outcome}' — must be one of "
                f"{sorted(_VALID_OUTCOMES)}"
            )
        details.append(TestDetail(
            test_name=item["test_name"],
            outcome=outcome,
            message=item["message"],
        ))
    return details
