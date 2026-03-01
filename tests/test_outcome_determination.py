"""Tests for determine_outcome() — Issue #233.

Covers:
- Every TaskState value mapped to the correct PhaseOutcome
- Timeout detection via the errors list (code="timeout")
- Edge cases: missing state, empty dict, None state, unknown state values
- TaskError objects (not just dicts) in the errors list
- Re-export from schemas module
"""

from __future__ import annotations

import pytest

from orchestration_engine.transitions import PhaseOutcome, determine_outcome


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _result(state: str, errors=None, **kwargs) -> dict:
    """Build a minimal result dict resembling TaskResult.model_dump()."""
    d = {"state": state, "result": {}, "confidence": 0.9}
    if errors is not None:
        d["errors"] = errors
    d.update(kwargs)
    return d


def _timeout_error(code: str = "timeout") -> dict:
    """Return a TaskError dict with the given code."""
    return {"code": code, "message": "timed out", "severity": "error"}


# ---------------------------------------------------------------------------
# 1. Happy-path mappings for every TaskState value
# ---------------------------------------------------------------------------

class TestDetermineOutcomeMappings:
    """Each TaskState string maps to the expected PhaseOutcome."""

    def test_success_maps_to_success(self):
        assert determine_outcome(_result("success")) == PhaseOutcome.SUCCESS

    def test_failed_maps_to_failed(self):
        assert determine_outcome(_result("failed")) == PhaseOutcome.FAILED

    def test_permanently_failed_maps_to_failed(self):
        assert determine_outcome(_result("permanently_failed")) == PhaseOutcome.FAILED

    def test_retry_maps_to_failed(self):
        assert determine_outcome(_result("retry")) == PhaseOutcome.FAILED

    def test_queued_maps_to_failed(self):
        """QUEUED is an unexpected terminal state — safe failure."""
        assert determine_outcome(_result("queued")) == PhaseOutcome.FAILED

    def test_running_maps_to_failed(self):
        """RUNNING means execution was incomplete — safe failure."""
        assert determine_outcome(_result("running")) == PhaseOutcome.FAILED

    def test_cancelled_maps_to_skipped(self):
        assert determine_outcome(_result("cancelled")) == PhaseOutcome.SKIPPED


# ---------------------------------------------------------------------------
# 2. Timeout detection
# ---------------------------------------------------------------------------

class TestTimeoutDetection:
    """A FAILED state with a timeout error code yields TIMEOUT."""

    def test_failed_with_timeout_error_dict_yields_timeout(self):
        result = _result("failed", errors=[_timeout_error()])
        assert determine_outcome(result) == PhaseOutcome.TIMEOUT

    def test_failed_with_multiple_errors_one_timeout_yields_timeout(self):
        result = _result("failed", errors=[
            {"code": "rate_limited", "message": "429", "severity": "error"},
            _timeout_error(),
        ])
        assert determine_outcome(result) == PhaseOutcome.TIMEOUT

    def test_failed_with_non_timeout_error_yields_failed(self):
        result = _result("failed", errors=[
            {"code": "rate_limited", "message": "429", "severity": "error"},
        ])
        assert determine_outcome(result) == PhaseOutcome.FAILED

    def test_failed_with_empty_errors_list_yields_failed(self):
        result = _result("failed", errors=[])
        assert determine_outcome(result) == PhaseOutcome.FAILED

    def test_failed_with_no_errors_key_yields_failed(self):
        result = {"state": "failed", "result": {}}
        assert determine_outcome(result) == PhaseOutcome.FAILED

    def test_timeout_error_code_is_case_insensitive(self):
        """Error codes stored with different casing are still detected."""
        result = _result("failed", errors=[{"code": "TIMEOUT", "message": "t/o", "severity": "error"}])
        assert determine_outcome(result) == PhaseOutcome.TIMEOUT

    def test_timeout_on_non_failed_state_not_triggered(self):
        """Timeout errors only matter when state is 'failed'."""
        # A 'cancelled' result with a timeout error should still be SKIPPED
        result = _result("cancelled", errors=[_timeout_error()])
        assert determine_outcome(result) == PhaseOutcome.SKIPPED

    def test_success_with_timeout_error_still_success(self):
        """A success result is never overridden by errors."""
        result = _result("success", errors=[_timeout_error()])
        assert determine_outcome(result) == PhaseOutcome.SUCCESS

    def test_timeout_error_as_object_with_code_attr(self):
        """errors may contain objects (e.g. TaskError) with a .code attribute."""
        class FakeError:
            def __init__(self, code):
                self.code = code

        result = _result("failed", errors=[FakeError("timeout")])
        assert determine_outcome(result) == PhaseOutcome.TIMEOUT

    def test_non_timeout_error_object_yields_failed(self):
        class FakeError:
            def __init__(self, code):
                self.code = code

        result = _result("failed", errors=[FakeError("network_error")])
        assert determine_outcome(result) == PhaseOutcome.FAILED


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------

class TestDetermineOutcomeEdgeCases:
    """Robustness: missing keys, None values, unknown states."""

    def test_empty_dict_yields_failed(self):
        assert determine_outcome({}) == PhaseOutcome.FAILED

    def test_missing_state_key_yields_failed(self):
        assert determine_outcome({"result": {}, "confidence": 1.0}) == PhaseOutcome.FAILED

    def test_none_state_value_yields_failed(self):
        assert determine_outcome({"state": None}) == PhaseOutcome.FAILED

    def test_empty_string_state_yields_failed(self):
        assert determine_outcome({"state": ""}) == PhaseOutcome.FAILED

    def test_unknown_state_string_yields_failed(self):
        assert determine_outcome({"state": "exploded"}) == PhaseOutcome.FAILED

    def test_state_with_extra_whitespace_normalised(self):
        """Strings like '  success  ' should still resolve correctly."""
        assert determine_outcome({"state": "  success  "}) == PhaseOutcome.SUCCESS
        assert determine_outcome({"state": "  failed  "}) == PhaseOutcome.FAILED
        assert determine_outcome({"state": "  cancelled  "}) == PhaseOutcome.SKIPPED

    def test_state_uppercase_normalised(self):
        """State values stored in uppercase (e.g. from TaskState.value) resolve."""
        assert determine_outcome({"state": "SUCCESS"}) == PhaseOutcome.SUCCESS
        assert determine_outcome({"state": "FAILED"}) == PhaseOutcome.FAILED
        assert determine_outcome({"state": "CANCELLED"}) == PhaseOutcome.SKIPPED

    def test_state_mixed_case_normalised(self):
        assert determine_outcome({"state": "Failed"}) == PhaseOutcome.FAILED
        assert determine_outcome({"state": "Cancelled"}) == PhaseOutcome.SKIPPED

    def test_none_errors_key_does_not_raise(self):
        """errors=None should be treated the same as an empty list."""
        result = {"state": "failed", "errors": None}
        assert determine_outcome(result) == PhaseOutcome.FAILED

    def test_extra_keys_are_ignored(self):
        """Extra result payload does not affect outcome."""
        result = {
            "state": "success",
            "task_id": "abc-123",
            "result": {"text": "hello"},
            "metadata": {"attempt": 1},
            "confidence": 0.95,
        }
        assert determine_outcome(result) == PhaseOutcome.SUCCESS

    def test_returns_phase_outcome_type(self):
        """Return type is always PhaseOutcome, not a plain string."""
        outcome = determine_outcome({"state": "success"})
        assert isinstance(outcome, PhaseOutcome)

    def test_all_outcomes_reachable(self):
        """All four PhaseOutcome values can be produced by determine_outcome."""
        outcomes = {
            determine_outcome({"state": "success"}),
            determine_outcome({"state": "failed"}),
            determine_outcome({"state": "cancelled"}),
            determine_outcome({"state": "failed", "errors": [{"code": "timeout"}]}),
        }
        assert outcomes == set(PhaseOutcome)


# ---------------------------------------------------------------------------
# 4. Re-export from schemas module
# ---------------------------------------------------------------------------

class TestSchemaReExport:
    """PhaseOutcome and determine_outcome are accessible via schemas module."""

    def test_phase_outcome_importable_from_schemas(self):
        from orchestration_engine.schemas import PhaseOutcome as PO  # noqa: F401
        assert PO.SUCCESS == "success"

    def test_determine_outcome_importable_from_schemas(self):
        from orchestration_engine.schemas import determine_outcome as do
        assert do({"state": "success"}) == PhaseOutcome.SUCCESS

    def test_phase_outcome_same_object_as_transitions(self):
        """schemas.PhaseOutcome should be the same class (not a copy)."""
        from orchestration_engine.schemas import PhaseOutcome as PO
        from orchestration_engine.transitions import PhaseOutcome as PO2
        assert PO is PO2
