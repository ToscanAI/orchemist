## User Story
As the orchestrator, I want a structured IPC protocol between the orchestrator and the external validator so that validation requests and results are schema-validated, typed, and auditable.


## Context
The orchestrator and external validator (#539) communicate over stdin/stdout pipes. Raw text output is fragile and untyped. We need a schema-validated protocol for structured request/response with clear error semantics.

JSON-RPC 2.0 over stdin/stdout is the chosen protocol: structured, stateless, standard, auditable, and requires no HTTP server or sockets.

### Files to Create
- `src/orchestration_engine/ipc.py` — Protocol layer with request/response dataclasses, serialization, deserialization, and schema validation
- `tests/test_ipc.py` — Unit tests for all serialization paths, schema validation, and error handling

### Protocol Methods
- `validate` — Run acceptance tests. Returns structured verdict with pass/fail/retry.
- `health` — Ping. Returns `{"status": "ok"}`. Used by orchestrator to verify subprocess is alive.

### Exception Design
`IPCProtocolError` is a **standalone `Exception` subclass** — NOT an `OrchestratorError`. This keeps `ipc.py` as a pure data layer with zero engine imports. The validator subprocess (`validator_runner.py` from #539) must be able to import `ipc.py` without pulling in `sequencer.py`, `daemon.py`, or any engine internals via transitive dependencies through `errors.py`.

### IPCError Dataclass

```python
@dataclass
class IPCError:
    code: int                # JSON-RPC error code (see table below)
    message: str             # Human-readable error description
    data: Optional[str]      # Additional error context; None if not provided
```

**Error code ranges (JSON-RPC 2.0 standard + application-defined):**

| Code | Meaning | Source |
|------|---------|--------|
| -32700 | Parse error (malformed JSON) | JSON-RPC 2.0 reserved |
| -32600 | Invalid request (missing jsonrpc/method) | JSON-RPC 2.0 reserved |
| -32601 | Method not found | JSON-RPC 2.0 reserved |
| -32602 | Invalid params (missing required, wrong type) | JSON-RPC 2.0 reserved |
| 1 | Test execution crashed | Application-defined |
| 2 | Test execution timed out | Application-defined |
| 3 | Repository checkout failed | Application-defined |
| 4 | Test store not found or corrupted | Application-defined |

Application-defined codes use range 1–99. Codes outside reserved and application ranges are accepted by `deserialize_response` without validation (forward-compatible).

### Request ID Strategy
`serialize_request` accepts an optional `request_id: int` parameter (defaults to auto-incrementing internal counter). `deserialize_response` does **not** enforce id matching — the caller (`validator.py` in #539) may verify it if needed. This keeps us JSON-RPC 2.0 compliant without overengineering for the single-subprocess model.

### Request ID Preservation
Both `ValidationRequest` and `HealthRequest` dataclasses include an `id: int` field, populated by `deserialize_request` from the JSON-RPC `id` field. This allows the validator subprocess to echo the correct id back via `serialize_response(result, request.id)` without re-parsing the raw JSON.

```python
# Validator subprocess flow:
line = sys.stdin.readline()
request = deserialize_request(line)       # request.id preserved
result = run_tests(request)
sys.stdout.write(serialize_response(result, request.id))
```

### Return Type Design
`deserialize_response` returns `Union[ValidationResult, HealthResult, IPCError]`. The caller uses `isinstance` to branch:

```python
result = deserialize_response(line)
if isinstance(result, IPCError):
    handle_error(result)
elif isinstance(result, ValidationResult):
    process_verdict(result)
elif isinstance(result, HealthResult):
    log_health(result)
```

This keeps `ipc.py` as a pure data layer — no exceptions for expected protocol outcomes (error responses are valid JSON-RPC, not protocol violations). `IPCProtocolError` is raised only for malformed wire data.

### Response Discrimination Strategy
JSON-RPC 2.0 responses do not echo the method name. `deserialize_response` discriminates result types by **structural field presence**:

| Field in `result` | Returned type |
|-------------------|---------------|
| `verdict` present | `ValidationResult` |
| `status` present (no `verdict`) | `HealthResult` |
| Neither `verdict` nor `status` | Raises `IPCProtocolError` with message containing "unrecognized result shape" |
| `error` key (no `result` key) | `IPCError` |

This is deterministic and requires no method-tracking state.

### Request Format
```json
{
  "jsonrpc": "2.0",
  "method": "validate",
  "params": {
    "run_id": "abc-123",
    "test_store_path": "/var/orchemist/test_store/abc-123",
    "repo_path": "/tmp/orchemist/abc-123",
    "branch": "fix-auth-bug",
    "test_command": "pytest",
    "timeout_seconds": 300,
    "test_manifest_hash": "sha256:abc123..."
  },
  "id": 1
}
```

### Health Request/Response Format
```json
// Request
{"jsonrpc": "2.0", "method": "health", "id": 2}

// Response
{"jsonrpc": "2.0", "result": {"status": "ok"}, "id": 2}
```

### Error Response Format
```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": 2,
    "message": "Test execution timed out after 300 seconds",
    "data": "pytest process killed by SIGTERM"
  },
  "id": 1
}
```

### Response Format (Validate)
```json
{
  "jsonrpc": "2.0",
  "result": {
    "verdict": "FAIL",
    "tests_total": 12,
    "tests_passed": 9,
    "tests_failed": 3,
    "tests_errored": 0,
    "pass_rate": 0.75,
    "duration_seconds": 14.2,
    "details": [
      {"test_name": "test_auth_timeout", "outcome": "FAIL", "message": "Expected 401, got 500"},
      {"test_name": "test_session_expiry", "outcome": "FAIL", "message": "Session not invalidated after TTL"},
      {"test_name": "test_rate_limit", "outcome": "FAIL", "message": "Expected 429 after 100 requests"}
    ],
    "retry_recommended": true,
    "retry_reason": "3 behavioral contracts failed: auth timeout, session expiry, rate limit",
    "test_manifest_hash": "sha256:abc123..."
  },
  "id": 1
}
```

### Details Field Schema
Each item in the `details` list is a `TestDetail` dataclass:

```python
@dataclass
class TestDetail:
    test_name: str           # pytest node id or test function name
    outcome: str             # "PASS" | "FAIL" | "ERROR"
    message: str             # failure message; empty string for passing tests
```

### HealthResult Dataclass

```python
@dataclass
class HealthResult:
    status: str              # "ok"
```

### Required vs Optional Params

**Request params:**
| Param | Required | Default | Notes |
|-------|----------|---------|-------|
| `run_id` | **required** | — | Pipeline run identifier |
| `test_store_path` | **required** | — | Path to immutable test store |
| `repo_path` | **required** | — | Path to the repository |
| `branch` | **required** | — | Git branch for checkout |
| `test_command` | optional | `"pytest"` | Command to execute tests |
| `timeout_seconds` | optional | `300` | Max seconds for test execution |
| `test_manifest_hash` | optional | `None` | Integrity check hash; not always available |

**Response fields (validate):**
| Field | Required | Notes |
|-------|----------|-------|
| `verdict` | **required** | "PASS", "FAIL", or "ERROR" |
| `tests_total` | **required** | Total test count |
| `tests_passed` | **required** | — |
| `tests_failed` | **required** | — |
| `tests_errored` | **required** | — |
| `pass_rate` | **required** | 0.0–1.0 |
| `duration_seconds` | **required** | Wall clock time |
| `details` | **required** | `List[TestDetail]` (may be empty) |
| `retry_recommended` | **required** | Boolean |
| `retry_reason` | optional | `""` if retry not recommended |
| `test_manifest_hash` | optional | Echo of request hash if provided |

**Response fields (health):**
| Field | Required | Notes |
|-------|----------|-------|
| `status` | **required** | `"ok"` |

### Line Framing
Line framing is the **caller's responsibility**. `deserialize_request` / `deserialize_response` assume clean single-line input (the caller reads via `readline()`). If malformed multi-line input is passed as a single string, the JSON parser will naturally raise an error caught as `IPCProtocolError("invalid JSON")`. No separate embedded-newline check needed.

### Serialization Function Summary

| Function | Input | Output | Used by |
|----------|-------|--------|---------|
| `serialize_request(request, request_id?)` | `ValidationRequest` or `HealthRequest` | JSON-RPC line (str) | Orchestrator (validator.py) |
| `deserialize_request(line)` | str | `ValidationRequest` or `HealthRequest` | Validator subprocess (validator_runner.py) |
| `serialize_response(result, request_id)` | `ValidationResult` or `HealthResult` | JSON-RPC line (str) | Validator subprocess (validator_runner.py) |
| `serialize_error_response(error, request_id)` | `IPCError` | JSON-RPC error line (str) | Validator subprocess (validator_runner.py) |
| `deserialize_response(line)` | str | `Union[ValidationResult, HealthResult, IPCError]` | Orchestrator (validator.py) |


## Integration points
- Reads from: N/A (pure data layer — no file I/O, no imports from existing engine modules)
- Extends: N/A (standalone module — does not subclass or modify existing classes; mirrors `TestRunResult` dataclass pattern from `src/orchestration_engine/test_runner.py`)
- New files: `src/orchestration_engine/ipc.py`, `tests/test_ipc.py`
- Used by: `validator.py` (#539, orchestrator side), `validator_runner.py` (#539, subprocess side)
- Depends on: nothing (pure data layer — `IPCProtocolError` subclasses `Exception`, not `OrchestratorError`)
