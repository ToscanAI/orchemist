## Behavioral Contracts

### Happy path — Validate method
- Given a valid `ValidationRequest` with method "validate" and all required params, when `serialize_request(request)` is called, the system returns a single JSON line conforming to JSON-RPC 2.0 spec with `jsonrpc`, `method`, `params`, and `id` fields.
- Given a valid JSON-RPC 2.0 response line with a `result` object containing all `ValidationResult` fields, when `deserialize_response(line)` is called, the system returns a `ValidationResult` dataclass with all fields populated.
- Given a valid JSON-RPC 2.0 response line with an `error` object, when `deserialize_response(line)` is called, the system returns an `IPCError` dataclass with `code`, `message`, and `data` fields.
- Given `serialize_response(result, request_id)` is called with a `ValidationResult`, the system returns a single JSON line conforming to JSON-RPC 2.0 response spec with the provided `request_id` as the `id` field.
- Given `deserialize_request(line)` receives a valid validate request, the system returns a `ValidationRequest` with all params parsed.
- Given a `ValidationResult` with a `details` list, each item is deserialized into a `TestDetail` dataclass with `test_name`, `outcome`, and `message` fields.

### Happy path — Health method
- Given a health request (`method: "health"`), when `serialize_request` is called, the system produces a valid JSON-RPC request with no params and an auto-generated or caller-supplied id.
- Given `deserialize_request(line)` receives a valid health request, the system returns a `HealthRequest` (distinct from `ValidationRequest`) with no params.
- Given `serialize_response(result, request_id)` is called with a `HealthResult(status="ok")`, the system returns a JSON-RPC response line with `{"result": {"status": "ok"}, "id": <request_id>}`.
- Given a valid JSON-RPC 2.0 response line with `result.status` field, when `deserialize_response(line)` is called, the system returns a `HealthResult` dataclass.

### Happy path — Error responses
- Given `serialize_error_response(error, request_id)` is called with an `IPCError` and a request id, the system returns a JSON-RPC 2.0 error response line with `error.code`, `error.message`, and optionally `error.data`.
- Given `serialize_error_response` is called with an `IPCError` where `data` is `None`, the `data` field is omitted from the serialized JSON (not included as `null`).

### Happy path — Request ID
- Given `serialize_request` is called without a `request_id`, the system auto-generates a monotonically increasing integer id starting from 1.
- Given `serialize_request` is called with an explicit `request_id=42`, the system uses 42 as the JSON-RPC id.
- Given `deserialize_request(line)` receives a valid request with `"id": 7`, the returned `ValidationRequest` or `HealthRequest` dataclass has `id=7`.

### Configuration
- Given a `ValidationRequest` with `timeout_seconds` omitted, the system defaults to 300 seconds.
- Given a `ValidationRequest` with `test_command` omitted, the system defaults to "pytest".
- Given a `ValidationRequest` with `test_manifest_hash` omitted, the field is `None` (not included in serialized params).

### Error handling
- Given a JSON line that is not valid JSON, `deserialize_response` raises `IPCProtocolError` with message containing "invalid JSON".
- Given a JSON-RPC response missing the `jsonrpc` field or with wrong version, `deserialize_response` raises `IPCProtocolError` with message containing "invalid JSON-RPC".
- Given a response with `result` missing required fields (verdict, tests_total), `deserialize_response` raises `IPCProtocolError` with message containing "missing required field".
- Given a request with unknown method (not "validate" or "health"), `deserialize_request` raises `IPCProtocolError` with message containing "unknown method".
- Given a validate request missing required params (run_id, test_store_path, repo_path, branch), `deserialize_request` raises `IPCProtocolError` with message containing "missing required param".
- Given a response with a `details` item missing `test_name`, `outcome`, or `message`, `deserialize_response` raises `IPCProtocolError` with message containing "invalid test detail".
- Given a response with a `details` item where `outcome` is not in ("PASS", "FAIL", "ERROR"), `deserialize_response` raises `IPCProtocolError` with message containing "invalid test detail outcome".

### Edge cases
- Given a response line with trailing whitespace or newlines, `deserialize_response` strips them before parsing.
- Given a response with `verdict` value not in ("PASS", "FAIL", "ERROR"), `deserialize_response` raises `IPCProtocolError` with message containing "invalid verdict".
- Given a response with `pass_rate` outside 0.0-1.0, `deserialize_response` raises `IPCProtocolError`.
- Given a response with `tests_total: 0` and `verdict: PASS`, this is valid (no tests = vacuous truth, the validator just reports it).
- Given very large detail payloads (>1MB JSON line), serialization and deserialization complete without error.
- Given a response with an empty `details` list (`[]`), deserialization succeeds with an empty list of `TestDetail`.
- Given an error response with an unknown application error code (e.g. code 50), `deserialize_response` returns an `IPCError` without raising — unknown codes are forward-compatible.
- Given a health response with `status` value other than `"ok"`, `deserialize_response` returns a `HealthResult` with the actual status value (no validation — the caller decides how to handle non-ok status).
- Given a JSON-RPC 2.0 response with a `result` object containing neither `verdict` nor `status`, `deserialize_response` raises `IPCProtocolError` with message containing "unrecognized result shape".


## Acceptance Criteria
- [ ] `serialize_request` produces valid JSON-RPC 2.0 request lines for validate method
- [ ] `serialize_request` produces valid JSON-RPC 2.0 request lines for health method (no params)
- [ ] `serialize_request` auto-generates monotonic ids when `request_id` is not provided
- [ ] `serialize_request` uses caller-supplied `request_id` when provided
- [ ] `deserialize_response` parses valid validate responses into `ValidationResult`
- [ ] `deserialize_response` parses valid health responses into `HealthResult`
- [ ] `deserialize_response` parses error responses into `IPCError`
- [ ] `deserialize_response` returns `Union[ValidationResult, HealthResult, IPCError]` — caller uses `isinstance`
- [ ] `deserialize_response` parses `details` items into `TestDetail` dataclasses
- [ ] `serialize_response` produces valid JSON-RPC 2.0 response lines for `ValidationResult`
- [ ] `serialize_response` produces valid JSON-RPC 2.0 response lines for `HealthResult`
- [ ] `serialize_error_response` produces valid JSON-RPC 2.0 error response lines from `IPCError`
- [ ] `serialize_error_response` omits `data` field when `IPCError.data` is `None`
- [ ] `deserialize_request` parses valid validate requests into `ValidationRequest`
- [ ] `deserialize_request` parses valid health requests into `HealthRequest`
- [ ] Rejects invalid JSON with `IPCProtocolError`
- [ ] Rejects wrong JSON-RPC version with `IPCProtocolError`
- [ ] Rejects missing required fields/params with `IPCProtocolError`
- [ ] Rejects unknown methods with `IPCProtocolError`
- [ ] Rejects invalid verdict values with `IPCProtocolError`
- [ ] Rejects invalid `details` items (missing fields or bad outcome) with `IPCProtocolError`
- [ ] Handles trailing whitespace gracefully
- [ ] Defaults timeout_seconds to 300, test_command to "pytest", test_manifest_hash to None when omitted
- [ ] `branch` is required — missing branch raises `IPCProtocolError`
- [ ] pass_rate outside 0.0-1.0 range is rejected with `IPCProtocolError`
- [ ] tests_total: 0 with verdict: PASS is accepted as valid
- [ ] Empty `details` list is accepted as valid
- [ ] Unknown application error codes accepted without raising (forward-compatible)
- [ ] Payloads larger than 1MB are handled without truncation
- [ ] Each serialized line is newline-terminated and contains no embedded newlines
- [ ] `IPCProtocolError` subclasses `Exception` (not `OrchestratorError`)
- [ ] `IPCError` dataclass has `code: int`, `message: str`, `data: Optional[str]`
- [ ] `HealthResult` dataclass has `status: str`
- [ ] `ipc.py` has zero imports from `orchestration_engine` modules
- [ ] `deserialize_request` populates `id` field on `ValidationRequest` and `HealthRequest` from JSON-RPC id
- [ ] Response discrimination uses structural field presence (`verdict` → ValidationResult, `status` → HealthResult)
- [ ] Unrecognized result shape (no `verdict`, no `status`) raises `IPCProtocolError`
- [ ] All existing tests pass with no regressions
