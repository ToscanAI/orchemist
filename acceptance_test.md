# Phase: acceptance_test

# Acceptance Test Phase — Summary

**Source:** `/tmp/pipeline-540/behavioral.md`
**Test file:** `/tmp/pipeline-540/acceptance_tests.py`
**Total tests written:** 23

---

## Behavioral Contracts Covered (one per test)

| # | Test Function | Behavioral Contract | Rationale |
|---|---------------|---------------------|-----------|
| 1 | `test_serialize_validate_request_produces_valid_jsonrpc` | `serialize_request` for a `ValidationRequest` returns a JSON-RPC 2.0 line with `jsonrpc`, `method`, `params`, `id` | Validates the core happy path for request serialization |
| 2 | `test_serialize_validate_request_line_is_newline_terminated` | Each serialized line is newline-terminated with no embedded newlines | Validates wire protocol framing requirement |
| 3 | `test_serialize_health_request_produces_valid_jsonrpc_no_params` | `serialize_request` for `HealthRequest` produces JSON-RPC with no params and valid id | Validates health method path, distinct from validate |
| 4 | `test_serialize_request_autogenerates_monotonic_ids` | Without `request_id`, IDs are auto-generated as monotonically increasing integers | Validates the ID counter behavior when caller doesn't supply an id |
| 5 | `test_serialize_request_uses_explicit_request_id` | With `request_id=42`, serialized JSON uses `id: 42` | Validates that caller-supplied IDs are respected |
| 6 | `test_deserialize_response_returns_validation_result` | Valid JSON-RPC response with `verdict`, `tests_total`, `pass_rate` returns a `ValidationResult` | Core deserialization happy path for validate responses |
| 7 | `test_deserialize_response_parses_test_details_into_dataclasses` | `details` items are deserialized into `TestDetail` dataclasses with `test_name`, `outcome`, `message` | Validates nested dataclass deserialization |
| 8 | `test_deserialize_response_returns_health_result` | JSON-RPC response with `result.status` returns a `HealthResult` | Core deserialization happy path for health responses |
| 9 | `test_deserialize_response_health_result_preserves_non_ok_status` | `HealthResult` preserves non-"ok" status values without validation | Validates caller-side handling of degraded health |
| 10 | `test_deserialize_response_returns_ipc_error` | JSON-RPC error response returns `IPCError` with `code`, `message`, `data` | Core error deserialization happy path |
| 11 | `test_deserialize_response_unknown_error_code_does_not_raise` | Unknown error codes (e.g. `50`) return `IPCError` without raising | Forward-compatibility: new error codes must not break older clients |
| 12 | `test_serialize_response_for_validation_result` | `serialize_response(ValidationResult, request_id=7)` returns JSON-RPC response with `id: 7` | Server-side serialization: validates ValidationResult round-trip |
| 13 | `test_serialize_response_for_health_result` | `serialize_response(HealthResult(status="ok"), request_id=3)` returns `{"result": {"status": "ok"}, "id": 3}` | Server-side serialization: validates HealthResult round-trip |
| 14 | `test_serialize_error_response_omits_data_when_none` | `serialize_error_response` with `IPCError.data=None` omits the `data` key entirely | Spec mandates omission (not null) — important for client compatibility |
| 15 | `test_deserialize_request_parses_validation_request` | `deserialize_request` for a validate request returns `ValidationRequest` with all params and `id` | Server-side deserialization happy path |
| 16 | `test_deserialize_request_parses_health_request` | `deserialize_request` for a health request returns `HealthRequest` (distinct type), with `id` | Validates type discrimination between request types |
| 17 | `test_validation_request_defaults_timeout_and_test_command` | When `timeout_seconds`, `test_command`, `test_manifest_hash` are omitted, defaults to 300, "pytest", None | Validates configuration defaults |
| 18 | `test_deserialize_response_raises_on_invalid_json` | Non-JSON input raises `IPCProtocolError` containing "invalid JSON" | Core error handling |
| 19 | `test_deserialize_response_raises_on_wrong_jsonrpc_version` | Wrong/missing `jsonrpc` version raises `IPCProtocolError` containing "invalid JSON-RPC" | Protocol version enforcement |
| 20 | `test_deserialize_response_raises_on_missing_required_fields` | Result missing `verdict`/`tests_total` raises `IPCProtocolError` containing "missing required field" | Required field validation |
| 21 | `test_deserialize_request_raises_on_unknown_method` | Unknown method raises `IPCProtocolError` containing "unknown method" | Method whitelist enforcement |
| 22 | `test_deserialize_request_raises_on_missing_required_params` | Validate request missing `branch` raises `IPCProtocolError` containing "missing required param" | Required param validation, specifically `branch` |
| 23 | `test_deserialize_response_raises_on_invalid_test_detail_missing_field` | `details` item missing `message` raises `IPCProtocolError` containing "invalid test detail" | Detail field validation |
| 24 | `test_deserialize_response_raises_on_invalid_outcome_value` | `outcome: "SKIP"` (not in PASS/FAIL/ERROR) raises `IPCProtocolError` containing "invalid test detail outcome" | Outcome enum validation |
| 25 | `test_deserialize_response_raises_on_invalid_verdict` | `verdict: "UNKNOWN"` raises `IPCProtocolError` containing "invalid verdict" | Verdict enum validation |
| 26 | `test_deserialize_response_raises_on_pass_rate_out_of_range` | `pass_rate: 1.5` raises `IPCProtocolError` | Range validation for pass_rate (0.0–1.0) |
| 27 | `test_deserialize_response_raises_on_unrecognized_result_shape` | Result with neither `verdict` nor `status` raises `IPCProtocolError` containing "unrecognized result shape" | Structural discrimination validation |
| 28 | `test_deserialize_response_handles_trailing_whitespace` | Trailing whitespace/newlines on response lines are stripped before parsing | Robustness / real-world transport edge case |
| 29 | `test_deserialize_response_accepts_zero_tests_with_pass_verdict` | `tests_total=0`, `verdict=PASS` is valid (vacuous truth) | Edge case: empty test suites should not fail |
| 30 | `test_deserialize_response_accepts_empty_details_list` | `details=[]` deserializes to empty list without error | Edge case: no test details is valid |
| 31 | `test_ipc_protocol_error_is_exception_not_orchestrator_error` | `IPCProtocolError` is a subclass of `Exception`, NOT `OrchestratorError` | Isolation requirement: ipc module must not depend on engine internals |
| 32 | `test_large_payload_serialization_and_deserialization` | >1MB JSON payloads serialize and deserialize without error | Performance/robustness boundary test |
| 33 | `test_ipc_module_has_no_orchestration_engine_imports` | `ipc.py` source contains no imports from other `orchestration_engine.*` modules | Isolation contract: ipc must be standalone |

---

## Ambiguities in the Spec — Assumptions Made

### 1. Constructor signatures for dataclasses
**Ambiguity:** The spec defines field names (e.g. `ValidationRequest` with `run_id`, `test_store_path`, `repo_path`, `branch`) but does not specify whether `request_id` is a constructor kwarg or set separately.
**Assumption:** `ValidationRequest(run_id=..., test_store_path=..., repo_path=..., branch=..., request_id=...)` and `HealthRequest(request_id=...)` use keyword arguments. Tests that omit `request_id` expect auto-generation.

### 2. Where IDs come from for requests
**Ambiguity:** The spec says "auto-generates a monotonically increasing integer id starting from 1" but doesn't clarify if this is a module-level counter or instance-level.
**Assumption:** Module-level counter (so two consecutive calls without explicit id produce increasing IDs across calls in the same process).

### 3. Import path for `ipc` module
**Ambiguity:** The spec says `ipc.py` has zero imports from `orchestration_engine` modules but doesn't specify the import path.
**Assumption:** The module is importable as `orchestration_engine.ipc` from the `src/` directory (consistent with the existing package structure).

### 4. `serialize_request` dispatch
**Ambiguity:** Does `serialize_request` accept both `ValidationRequest` and `HealthRequest` as a union input, or are they separate functions?
**Assumption:** Single `serialize_request` function dispatching by type (consistent with spec listing one function name for both).

### 5. `test_manifest_hash` in serialized params
**Ambiguity:** The spec says "field is None (not included in serialized params)" — unclear if this means absent from JSON or present as `null`.
**Assumption:** Absent from JSON (omitted entirely when None), consistent with the `data` field behavior in error responses.

### 6. `pass_rate` with `tests_total=0`
**Ambiguity:** If there are 0 tests, what is the valid `pass_rate`? The spec says `tests_total=0, verdict=PASS` is valid but doesn't specify `pass_rate`.
**Assumption:** `pass_rate=1.0` is used for the 0-test case (vacuous truth), which is in range `[0.0, 1.0]`.

### 7. `HealthRequest` params field
**Ambiguity:** The spec says "no params" for health. It's unclear if `params` should be absent from the JSON or present as `{}`.
**Assumption:** `params` should be absent or empty (`{}` or `[]` or absent). Test checks `obj.get("params") in (None, {}, [])`.

