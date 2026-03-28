"""
Acceptance tests for IPC protocol serialization/deserialization module.
Written from behavioral contracts ONLY — no implementation details assumed.
Tests are pre-implementation: they define expected behavior, not internals.
"""
import sys
import json
import pytest

sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / 'src'))

# All imports from the ipc module — exact names are behavioral (from spec)
from orchestration_engine.ipc import (
    serialize_request,
    deserialize_response,
    serialize_response,
    serialize_error_response,
    deserialize_request,
    ValidationRequest,
    HealthRequest,
    ValidationResult,
    HealthResult,
    IPCError,
    TestDetail,
    IPCProtocolError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_validation_request(**overrides):
    """Construct a minimal valid ValidationRequest."""
    defaults = dict(
        run_id="run-123",
        test_store_path="/tmp/store",
        repo_path="/tmp/repo",
        branch="main",
    )
    defaults.update(overrides)
    return ValidationRequest(**defaults)


def _json_line(obj: dict) -> str:
    """Create a newline-terminated JSON-RPC 2.0 response line."""
    return json.dumps(obj) + "\n"


def valid_validation_response(request_id=1, **overrides):
    result = {
        "verdict": "PASS",
        "tests_total": 5,
        "pass_rate": 1.0,
        "details": [],
    }
    result.update(overrides)
    return _json_line({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    })


def valid_health_response(request_id=1, status="ok"):
    return _json_line({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"status": status},
    })


def valid_error_response(request_id=1, code=-32600, message="Bad request", data=None):
    error_obj = {"code": code, "message": message}
    if data is not None:
        error_obj["data"] = data
    return _json_line({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error_obj,
    })


# ===========================================================================
# 1. serialize_request — validate method
# ===========================================================================

def test_serialize_validate_request_produces_valid_jsonrpc():
    """
    Behavioral contract: Given a valid ValidationRequest, serialize_request
    returns a single JSON line conforming to JSON-RPC 2.0 with jsonrpc,
    method, params, and id fields.
    """
    req = make_validation_request(request_id=1)
    line = serialize_request(req)

    obj = json.loads(line.strip())
    assert obj["jsonrpc"] == "2.0", "jsonrpc field must be '2.0'"
    assert obj["method"] == "validate", "method must be 'validate'"
    assert "params" in obj, "params field must be present"
    assert "id" in obj, "id field must be present"


def test_serialize_validate_request_line_is_newline_terminated():
    """
    Behavioral contract: Each serialized line is newline-terminated and
    contains no embedded newlines within the JSON payload itself.
    """
    req = make_validation_request()
    line = serialize_request(req)

    assert line.endswith("\n"), "serialized line must end with newline"
    # The JSON body itself should not contain embedded newlines
    assert line.count("\n") == 1, "exactly one newline (the terminator)"


# ===========================================================================
# 2. serialize_request — health method
# ===========================================================================

def test_serialize_health_request_produces_valid_jsonrpc_no_params():
    """
    Behavioral contract: Given a health request, serialize_request produces a
    valid JSON-RPC request with no params and a valid id.
    """
    req = HealthRequest(request_id=99)
    line = serialize_request(req)

    obj = json.loads(line.strip())
    assert obj["jsonrpc"] == "2.0"
    assert obj["method"] == "health"
    assert obj["id"] == 99
    # params should be absent or empty (no params for health)
    assert obj.get("params") in (None, {}, []), \
        "health request must have no params"


# ===========================================================================
# 3. Request ID — auto-generated (monotonically increasing)
# ===========================================================================

def test_serialize_request_autogenerates_monotonic_ids():
    """
    Behavioral contract: When serialize_request is called without a
    request_id, it auto-generates a monotonically increasing integer id
    starting from 1 (or the next integer in sequence).
    """
    req1 = make_validation_request()
    req2 = make_validation_request()

    line1 = serialize_request(req1)
    line2 = serialize_request(req2)

    id1 = json.loads(line1.strip())["id"]
    id2 = json.loads(line2.strip())["id"]

    assert isinstance(id1, int), "auto-generated id must be an integer"
    assert isinstance(id2, int), "auto-generated id must be an integer"
    assert id2 > id1, "auto-generated ids must be monotonically increasing"


def test_serialize_request_uses_explicit_request_id():
    """
    Behavioral contract: When serialize_request is called with an explicit
    request_id=42, the system uses 42 as the JSON-RPC id.
    """
    req = make_validation_request(request_id=42)
    line = serialize_request(req)
    obj = json.loads(line.strip())
    assert obj["id"] == 42


# ===========================================================================
# 4. deserialize_response — ValidationResult
# ===========================================================================

def test_deserialize_response_returns_validation_result():
    """
    Behavioral contract: Given a valid JSON-RPC 2.0 response line with a
    result object containing all ValidationResult fields, deserialize_response
    returns a ValidationResult dataclass with all fields populated.
    """
    line = valid_validation_response(verdict="PASS", tests_total=10, pass_rate=0.9)
    result = deserialize_response(line)

    assert isinstance(result, ValidationResult), \
        "must return ValidationResult for validate responses"
    assert result.verdict == "PASS"
    assert result.tests_total == 10
    assert abs(result.pass_rate - 0.9) < 1e-9


def test_deserialize_response_parses_test_details_into_dataclasses():
    """
    Behavioral contract: Given a ValidationResult with a details list, each
    item is deserialized into a TestDetail dataclass with test_name, outcome,
    and message fields.
    """
    detail = {"test_name": "test_foo", "outcome": "FAIL", "message": "assertion failed"}
    line = valid_validation_response(
        verdict="FAIL",
        tests_total=1,
        pass_rate=0.0,
        details=[detail],
    )
    result = deserialize_response(line)

    assert len(result.details) == 1
    td = result.details[0]
    assert isinstance(td, TestDetail)
    assert td.test_name == "test_foo"
    assert td.outcome == "FAIL"
    assert td.message == "assertion failed"


# ===========================================================================
# 5. deserialize_response — HealthResult
# ===========================================================================

def test_deserialize_response_returns_health_result():
    """
    Behavioral contract: Given a valid JSON-RPC 2.0 response line with
    result.status field, deserialize_response returns a HealthResult dataclass.
    """
    line = valid_health_response(status="ok")
    result = deserialize_response(line)

    assert isinstance(result, HealthResult), "must return HealthResult for health responses"
    assert result.status == "ok"


def test_deserialize_response_health_result_preserves_non_ok_status():
    """
    Behavioral contract: Given a health response with status value other than
    'ok', deserialize_response returns a HealthResult with the actual status
    value (no validation — the caller decides how to handle non-ok status).
    """
    line = valid_health_response(status="degraded")
    result = deserialize_response(line)

    assert isinstance(result, HealthResult)
    assert result.status == "degraded"


# ===========================================================================
# 6. deserialize_response — IPCError
# ===========================================================================

def test_deserialize_response_returns_ipc_error():
    """
    Behavioral contract: Given a valid JSON-RPC 2.0 response line with an
    error object, deserialize_response returns an IPCError dataclass with
    code, message, and data fields.
    """
    line = valid_error_response(code=-32600, message="Invalid Request", data="extra info")
    result = deserialize_response(line)

    assert isinstance(result, IPCError)
    assert result.code == -32600
    assert result.message == "Invalid Request"
    assert result.data == "extra info"


def test_deserialize_response_unknown_error_code_does_not_raise():
    """
    Behavioral contract: Given an error response with an unknown application
    error code (e.g. code 50), deserialize_response returns an IPCError
    without raising — unknown codes are forward-compatible.
    """
    line = valid_error_response(code=50, message="Future error")
    result = deserialize_response(line)

    assert isinstance(result, IPCError), "unknown error codes must not raise"
    assert result.code == 50


# ===========================================================================
# 7. serialize_response + serialize_error_response
# ===========================================================================

def test_serialize_response_for_validation_result():
    """
    Behavioral contract: serialize_response called with a ValidationResult
    returns a JSON-RPC 2.0 response line with the provided request_id as id.
    """
    vr = ValidationResult(verdict="PASS", tests_total=3, pass_rate=1.0, details=[])
    line = serialize_response(vr, request_id=7)
    obj = json.loads(line.strip())

    assert obj["jsonrpc"] == "2.0"
    assert obj["id"] == 7
    assert "result" in obj
    assert obj["result"]["verdict"] == "PASS"


def test_serialize_response_for_health_result():
    """
    Behavioral contract: serialize_response called with a HealthResult(status="ok")
    returns a JSON-RPC response line with {"result": {"status": "ok"}, "id": <request_id>}.
    """
    hr = HealthResult(status="ok")
    line = serialize_response(hr, request_id=3)
    obj = json.loads(line.strip())

    assert obj["jsonrpc"] == "2.0"
    assert obj["id"] == 3
    assert obj["result"]["status"] == "ok"


def test_serialize_error_response_omits_data_when_none():
    """
    Behavioral contract: serialize_error_response called with an IPCError
    where data is None must omit the data field from the serialized JSON
    (not included as null).
    """
    err = IPCError(code=-32700, message="Parse error", data=None)
    line = serialize_error_response(err, request_id=5)
    obj = json.loads(line.strip())

    assert obj["jsonrpc"] == "2.0"
    assert obj["id"] == 5
    assert "error" in obj
    assert obj["error"]["code"] == -32700
    assert "data" not in obj["error"], \
        "data field must be omitted (not null) when IPCError.data is None"


# ===========================================================================
# 8. deserialize_request
# ===========================================================================

def test_deserialize_request_parses_validation_request():
    """
    Behavioral contract: deserialize_request receiving a valid validate request
    returns a ValidationRequest with all params parsed, including the id field.
    """
    req_line = _json_line({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "validate",
        "params": {
            "run_id": "run-abc",
            "test_store_path": "/tmp/ts",
            "repo_path": "/tmp/repo",
            "branch": "feature/xyz",
        },
    })
    result = deserialize_request(req_line)

    assert isinstance(result, ValidationRequest)
    assert result.id == 7
    assert result.run_id == "run-abc"
    assert result.branch == "feature/xyz"


def test_deserialize_request_parses_health_request():
    """
    Behavioral contract: deserialize_request receiving a valid health request
    returns a HealthRequest (distinct from ValidationRequest) with no params,
    and id populated from the JSON-RPC id field.
    """
    req_line = _json_line({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "health",
    })
    result = deserialize_request(req_line)

    assert isinstance(result, HealthRequest)
    assert not isinstance(result, ValidationRequest), \
        "HealthRequest must be distinct from ValidationRequest"
    assert result.id == 2


# ===========================================================================
# 9. Default values for ValidationRequest
# ===========================================================================

def test_validation_request_defaults_timeout_and_test_command():
    """
    Behavioral contract: Given a ValidationRequest with timeout_seconds and
    test_command omitted, the system defaults to 300 seconds and 'pytest'.
    test_manifest_hash defaults to None.
    """
    # Serialize a request without optional fields and check defaults are applied
    req_line = _json_line({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "validate",
        "params": {
            "run_id": "run-defaults",
            "test_store_path": "/tmp/ts",
            "repo_path": "/tmp/repo",
            "branch": "main",
        },
    })
    result = deserialize_request(req_line)

    assert result.timeout_seconds == 300, "timeout_seconds must default to 300"
    assert result.test_command == "pytest", "test_command must default to 'pytest'"
    assert result.test_manifest_hash is None, "test_manifest_hash must default to None"


# ===========================================================================
# 10. Error handling — invalid JSON
# ===========================================================================

def test_deserialize_response_raises_on_invalid_json():
    """
    Behavioral contract: Given a JSON line that is not valid JSON,
    deserialize_response raises IPCProtocolError with message containing
    'invalid JSON'.
    """
    with pytest.raises(IPCProtocolError) as exc_info:
        deserialize_response("not valid JSON\n")

    assert "invalid JSON" in str(exc_info.value).lower()


def test_deserialize_response_raises_on_wrong_jsonrpc_version():
    """
    Behavioral contract: Given a JSON-RPC response missing the jsonrpc field
    or with wrong version, deserialize_response raises IPCProtocolError with
    message containing 'invalid JSON-RPC'.
    """
    bad_line = _json_line({
        "jsonrpc": "1.0",
        "id": 1,
        "result": {"verdict": "PASS", "tests_total": 0, "pass_rate": 1.0, "details": []},
    })
    with pytest.raises(IPCProtocolError) as exc_info:
        deserialize_response(bad_line)

    assert "invalid json-rpc" in str(exc_info.value).lower()


def test_deserialize_response_raises_on_missing_required_fields():
    """
    Behavioral contract: Given a response with result missing required fields
    (verdict, tests_total), deserialize_response raises IPCProtocolError with
    message containing 'missing required field'.
    """
    # Missing 'verdict' and 'tests_total'
    bad_line = _json_line({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"pass_rate": 0.5, "details": []},
    })
    with pytest.raises(IPCProtocolError) as exc_info:
        deserialize_response(bad_line)

    assert "missing required field" in str(exc_info.value).lower()


def test_deserialize_request_raises_on_unknown_method():
    """
    Behavioral contract: Given a request with unknown method (not 'validate'
    or 'health'), deserialize_request raises IPCProtocolError with message
    containing 'unknown method'.
    """
    bad_line = _json_line({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "reboot",
        "params": {},
    })
    with pytest.raises(IPCProtocolError) as exc_info:
        deserialize_request(bad_line)

    assert "unknown method" in str(exc_info.value).lower()


def test_deserialize_request_raises_on_missing_required_params():
    """
    Behavioral contract: Given a validate request missing required params
    (run_id, test_store_path, repo_path, branch), deserialize_request raises
    IPCProtocolError with message containing 'missing required param'.
    The branch field in particular is required.
    """
    # Missing 'branch' — a required param
    bad_line = _json_line({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "validate",
        "params": {
            "run_id": "run-1",
            "test_store_path": "/tmp/ts",
            "repo_path": "/tmp/repo",
            # branch missing
        },
    })
    with pytest.raises(IPCProtocolError) as exc_info:
        deserialize_request(bad_line)

    assert "missing required param" in str(exc_info.value).lower()


def test_deserialize_response_raises_on_invalid_test_detail_missing_field():
    """
    Behavioral contract: Given a response with a details item missing
    test_name, outcome, or message, deserialize_response raises
    IPCProtocolError with message containing 'invalid test detail'.
    """
    bad_detail = {"test_name": "test_foo", "outcome": "PASS"}  # missing 'message'
    bad_line = _json_line({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "verdict": "PASS",
            "tests_total": 1,
            "pass_rate": 1.0,
            "details": [bad_detail],
        },
    })
    with pytest.raises(IPCProtocolError) as exc_info:
        deserialize_response(bad_line)

    assert "invalid test detail" in str(exc_info.value).lower()


def test_deserialize_response_raises_on_invalid_outcome_value():
    """
    Behavioral contract: Given a response with a details item where outcome
    is not in ('PASS', 'FAIL', 'ERROR'), deserialize_response raises
    IPCProtocolError with message containing 'invalid test detail outcome'.
    """
    bad_detail = {"test_name": "test_foo", "outcome": "SKIP", "message": "skipped"}
    bad_line = _json_line({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "verdict": "PASS",
            "tests_total": 1,
            "pass_rate": 1.0,
            "details": [bad_detail],
        },
    })
    with pytest.raises(IPCProtocolError) as exc_info:
        deserialize_response(bad_line)

    assert "invalid test detail outcome" in str(exc_info.value).lower()


def test_deserialize_response_raises_on_invalid_verdict():
    """
    Behavioral contract: Given a response with verdict value not in
    ('PASS', 'FAIL', 'ERROR'), deserialize_response raises IPCProtocolError
    with message containing 'invalid verdict'.
    """
    bad_line = _json_line({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "verdict": "UNKNOWN",
            "tests_total": 1,
            "pass_rate": 0.5,
            "details": [],
        },
    })
    with pytest.raises(IPCProtocolError) as exc_info:
        deserialize_response(bad_line)

    assert "invalid verdict" in str(exc_info.value).lower()


def test_deserialize_response_raises_on_pass_rate_out_of_range():
    """
    Behavioral contract: Given a response with pass_rate outside 0.0-1.0,
    deserialize_response raises IPCProtocolError.
    """
    bad_line = _json_line({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "verdict": "PASS",
            "tests_total": 5,
            "pass_rate": 1.5,  # out of range
            "details": [],
        },
    })
    with pytest.raises(IPCProtocolError):
        deserialize_response(bad_line)


def test_deserialize_response_raises_on_unrecognized_result_shape():
    """
    Behavioral contract: Given a JSON-RPC 2.0 response with a result object
    containing neither 'verdict' nor 'status', deserialize_response raises
    IPCProtocolError with message containing 'unrecognized result shape'.
    """
    bad_line = _json_line({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"something_else": 42},
    })
    with pytest.raises(IPCProtocolError) as exc_info:
        deserialize_response(bad_line)

    assert "unrecognized result shape" in str(exc_info.value).lower()


# ===========================================================================
# 11. Edge cases
# ===========================================================================

def test_deserialize_response_handles_trailing_whitespace():
    """
    Behavioral contract: Given a response line with trailing whitespace or
    newlines, deserialize_response strips them before parsing.
    """
    line = valid_validation_response().rstrip() + "   \n\n"
    result = deserialize_response(line)

    assert isinstance(result, ValidationResult), \
        "trailing whitespace/newlines must be stripped gracefully"


def test_deserialize_response_accepts_zero_tests_with_pass_verdict():
    """
    Behavioral contract: Given a response with tests_total=0 and verdict=PASS,
    this is valid (no tests = vacuous truth). The system accepts it without error.
    """
    line = valid_validation_response(verdict="PASS", tests_total=0, pass_rate=1.0)
    result = deserialize_response(line)

    assert isinstance(result, ValidationResult)
    assert result.tests_total == 0
    assert result.verdict == "PASS"


def test_deserialize_response_accepts_empty_details_list():
    """
    Behavioral contract: Given a response with an empty details list ([]),
    deserialization succeeds with an empty list of TestDetail.
    """
    line = valid_validation_response(details=[])
    result = deserialize_response(line)

    assert isinstance(result, ValidationResult)
    assert result.details == [], "empty details list must be preserved as empty list"


def test_ipc_protocol_error_is_exception_not_orchestrator_error():
    """
    Behavioral contract: IPCProtocolError subclasses Exception (not
    OrchestratorError). The ipc module has zero imports from orchestration_engine
    modules — IPCProtocolError must be a standalone Exception subclass.
    """
    err = IPCProtocolError("test")
    assert isinstance(err, Exception), "IPCProtocolError must subclass Exception"

    # Verify it is NOT a subclass of any orchestration_engine-specific error
    # by checking its MRO doesn't include engine-specific types
    mro_names = [cls.__name__ for cls in type(err).__mro__]
    assert "OrchestratorError" not in mro_names, \
        "IPCProtocolError must NOT subclass OrchestratorError"


def test_large_payload_serialization_and_deserialization():
    """
    Behavioral contract: Given very large detail payloads (>1MB JSON line),
    serialization and deserialization complete without error.
    """
    # Build a large details list: ~1000 items, each with a ~1KB message
    large_message = "x" * 1024
    many_details = [
        {"test_name": f"test_{i}", "outcome": "PASS", "message": large_message}
        for i in range(1000)
    ]

    # Serialize a ValidationResult with large details
    vr = ValidationResult(
        verdict="PASS",
        tests_total=1000,
        pass_rate=1.0,
        details=[TestDetail(test_name=f"test_{i}", outcome="PASS", message=large_message)
                 for i in range(1000)],
    )
    line = serialize_response(vr, request_id=1)

    # Must be > 1MB
    assert len(line.encode()) > 1_000_000, "payload must exceed 1MB for this test"

    # Deserialization must succeed without truncation
    result = deserialize_response(line)
    assert isinstance(result, ValidationResult)
    assert len(result.details) == 1000


def test_ipc_module_has_no_orchestration_engine_imports():
    """
    Behavioral contract: ipc.py has zero imports from orchestration_engine
    modules — it must be a standalone, self-contained module.
    """
    import importlib
    import orchestration_engine.ipc as ipc_module

    # Inspect the module's globals for any orchestration_engine submodule references
    import sys as _sys
    # The ipc module itself should not import from orchestration_engine.*
    # We verify by checking the ipc module's __spec__ and imported names
    ipc_imports = set(vars(ipc_module).keys())

    # Get all loaded orchestration_engine sub-modules
    oe_submodules = {
        name for name in _sys.modules
        if name.startswith("orchestration_engine.") and name != "orchestration_engine.ipc"
    }

    # The ipc module should not have references to other orchestration_engine modules
    # Check by looking at the source file imports
    import inspect
    src_lines = inspect.getsource(ipc_module)
    for submod in ["orchestration_engine.models", "orchestration_engine.runner",
                   "orchestration_engine.scoring", "orchestration_engine.cli"]:
        assert submod not in src_lines, \
            f"ipc.py must not import from {submod}"
