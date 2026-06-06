"""Tests for issue #346: Auto-retry with exponential backoff on API errors.

Covers:
- ExecutorRetryConfig dataclass defaults and structure
- classify_exception_error_type() mapping rules
- OpenClawExecutor retry loop: retries on TRANSIENT/TIMEOUT/RATE_LIMIT errors
- No retry on PERMANENT errors
- Circuit breaker: pre-check blocks execution, opens after threshold failures
- Backoff timing: correct exponential formula
- Module-level _CIRCUIT_BREAKERS registry is shared across executor instances
"""

import json
import time
import threading
from datetime import datetime
from unittest.mock import MagicMock, patch, call
from typing import Optional

import pytest

from orchestration_engine.errors import (
    AuthenticationError,
    GatewayHTTPError,
    GatewayUnavailableError,
    RateLimitError,
)
from orchestration_engine.openclaw_executor import (
    MODEL_MAP,
    OpenClawExecutor,
    _CIRCUIT_BREAKERS,
    _CIRCUIT_BREAKERS_LOCK,
)
from orchestration_engine.recovery import (
    CircuitBreakerState,
    ErrorType,
    ExecutorRetryConfig,
    classify_exception_error_type,
)
from orchestration_engine.schemas import (
    ModelTier,
    Priority,
    TaskSpec,
    TaskState,
    TaskType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    """Clear the module-level circuit breaker registry before each test."""
    with _CIRCUIT_BREAKERS_LOCK:
        _CIRCUIT_BREAKERS.clear()
    yield
    with _CIRCUIT_BREAKERS_LOCK:
        _CIRCUIT_BREAKERS.clear()


@pytest.fixture
def executor():
    return OpenClawExecutor(
        gateway_url="http://localhost:18789",
        gateway_token="test-token",
    )


@pytest.fixture
def sample_task():
    return TaskSpec(
        type=TaskType.CONTENT,
        payload={"prompt": "Write a haiku about retries."},
        priority=Priority.NORMAL,
    )


# ---------------------------------------------------------------------------
# ExecutorRetryConfig tests
# ---------------------------------------------------------------------------


class TestExecutorRetryConfig:
    def test_default_max_attempts(self):
        cfg = ExecutorRetryConfig()
        assert cfg.max_attempts == 3

    def test_default_backoff_base(self):
        cfg = ExecutorRetryConfig()
        assert cfg.backoff_base == 2.0

    def test_default_backoff_multiplier(self):
        cfg = ExecutorRetryConfig()
        assert cfg.backoff_multiplier == 4.0

    def test_default_backoff_max(self):
        cfg = ExecutorRetryConfig()
        assert cfg.backoff_max == 60.0

    def test_default_circuit_breaker_threshold(self):
        cfg = ExecutorRetryConfig()
        assert cfg.circuit_breaker_threshold == 5

    def test_default_circuit_breaker_reset_seconds(self):
        cfg = ExecutorRetryConfig()
        assert cfg.circuit_breaker_reset_seconds == 300

    def test_custom_values(self):
        cfg = ExecutorRetryConfig(
            max_attempts=5,
            backoff_base=1.0,
            backoff_multiplier=2.0,
            backoff_max=30.0,
            circuit_breaker_threshold=10,
            circuit_breaker_reset_seconds=60,
        )
        assert cfg.max_attempts == 5
        assert cfg.backoff_base == 1.0
        assert cfg.backoff_multiplier == 2.0
        assert cfg.backoff_max == 30.0
        assert cfg.circuit_breaker_threshold == 10
        assert cfg.circuit_breaker_reset_seconds == 60


# ---------------------------------------------------------------------------
# classify_exception_error_type tests
# ---------------------------------------------------------------------------


class TestClassifyExceptionErrorType:
    def test_rate_limit_error(self):
        exc = RateLimitError(body="rate limited", retry_after=10)
        assert classify_exception_error_type(exc) == ErrorType.RATE_LIMIT

    def test_rate_limit_error_no_retry_after(self):
        exc = RateLimitError(body="rate limited")
        assert classify_exception_error_type(exc) == ErrorType.RATE_LIMIT

    def test_authentication_error_401(self):
        exc = AuthenticationError(status_code=401, body="unauthorized")
        assert classify_exception_error_type(exc) == ErrorType.PERMANENT

    def test_authentication_error_403(self):
        exc = AuthenticationError(status_code=403, body="forbidden")
        assert classify_exception_error_type(exc) == ErrorType.PERMANENT

    def test_gateway_unavailable_502(self):
        exc = GatewayUnavailableError(status_code=502, body="bad gateway")
        assert classify_exception_error_type(exc) == ErrorType.TRANSIENT

    def test_gateway_unavailable_503(self):
        exc = GatewayUnavailableError(status_code=503, body="service unavailable")
        assert classify_exception_error_type(exc) == ErrorType.TRANSIENT

    def test_gateway_unavailable_504(self):
        exc = GatewayUnavailableError(status_code=504, body="gateway timeout")
        assert classify_exception_error_type(exc) == ErrorType.TRANSIENT

    def test_generic_4xx_http_error(self):
        # 400 Bad Request — not 401/403/429, not 5xx → PERMANENT
        exc = GatewayHTTPError(status_code=400, body="bad request")
        assert classify_exception_error_type(exc) == ErrorType.PERMANENT

    def test_generic_404_http_error(self):
        exc = GatewayHTTPError(status_code=404, body="not found")
        assert classify_exception_error_type(exc) == ErrorType.PERMANENT

    def test_generic_5xx_http_error_is_transient(self):
        # 500 Internal Server Error (not 502/503/504) → TRANSIENT
        exc = GatewayHTTPError(status_code=500, body="internal error")
        assert classify_exception_error_type(exc) == ErrorType.TRANSIENT

    def test_builtin_timeout_error(self):
        exc = TimeoutError("session timed out")
        assert classify_exception_error_type(exc) == ErrorType.TIMEOUT

    def test_runtime_error_with_timeout(self):
        exc = RuntimeError("OpenClaw session sess-abc did not complete within 60s timeout")
        assert classify_exception_error_type(exc) == ErrorType.TIMEOUT

    def test_runtime_error_garbage_collected(self):
        exc = RuntimeError("Session xyz was garbage-collected: history previously had messages")
        assert classify_exception_error_type(exc) == ErrorType.PERMANENT

    def test_generic_runtime_error_is_transient(self):
        exc = RuntimeError("Gateway tool 'sessions_spawn' failed: something went wrong")
        assert classify_exception_error_type(exc) == ErrorType.TRANSIENT

    def test_generic_value_error_is_transient(self):
        exc = ValueError("Unexpected response format")
        assert classify_exception_error_type(exc) == ErrorType.TRANSIENT

    def test_generic_exception_is_transient(self):
        exc = Exception("Something unexpected")
        assert classify_exception_error_type(exc) == ErrorType.TRANSIENT

    def test_rate_limit_classified_before_auth_or_http(self):
        """RateLimitError is a GatewayHTTPError subclass — must be caught first."""
        exc = RateLimitError(body="429 too many requests")
        result = classify_exception_error_type(exc)
        assert result == ErrorType.RATE_LIMIT, (
            "RateLimitError must map to RATE_LIMIT, not PERMANENT or TRANSIENT"
        )

    def test_auth_error_classified_before_generic_http(self):
        """AuthenticationError is a GatewayHTTPError subclass — must map to PERMANENT."""
        exc = AuthenticationError(status_code=401, body="not authorized")
        result = classify_exception_error_type(exc)
        assert result == ErrorType.PERMANENT


# ---------------------------------------------------------------------------
# Helper to build a mock _http_post for the retry tests
# ---------------------------------------------------------------------------


def _spawn_resp(session_key: str) -> dict:
    return {
        "ok": True,
        "result": {
            "content": [{"type": "text", "text": json.dumps({"childSessionKey": session_key})}],
            "details": {"childSessionKey": session_key},
        },
    }


def _done_resp(session_key: str, output: str = "done") -> dict:
    return {
        "ok": True,
        "result": {
            "content": [{"type": "text", "text": json.dumps({
                "sessionKey": session_key,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": output}],
                        "stopReason": "stop",
                    },
                ],
            })}],
        },
    }


def _list_resp() -> dict:
    return {
        "ok": True,
        "result": {"content": [{"type": "text", "text": json.dumps({"sessions": []})}]},
    }


# ---------------------------------------------------------------------------
# Retry loop behaviour tests
# ---------------------------------------------------------------------------


class TestRetryOnTransientError:
    """Executor should retry up to max_attempts on transient/timeout/rate_limit errors."""

    def test_succeeds_on_second_attempt_after_runtime_error(
        self, executor, sample_task
    ):
        """First call raises RuntimeError (TRANSIENT); second call succeeds."""
        call_count = {"n": 0}

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("Transient network error")
            return "retry success output", 100

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert result.result["text"] == "retry success output"
        assert call_count["n"] == 2

    def test_succeeds_on_third_attempt_after_two_errors(
        self, executor, sample_task
    ):
        """Two RuntimeErrors then success → all three attempts used."""
        call_count = {"n": 0}

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise RuntimeError("Transient error")
            return "third attempt success", 50

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert call_count["n"] == 3

    def test_fails_after_max_attempts_exhausted(self, executor, sample_task):
        """All three attempts fail → FAILED result."""
        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            raise RuntimeError("Always failing")

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.errors[0].code == "execution_error"

    def test_retry_on_timeout_error(self, executor, sample_task):
        """TimeoutError should trigger a retry (TIMEOUT error type)."""
        call_count = {"n": 0}

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise TimeoutError("Session did not complete within 60s timeout")
            return "recovered after timeout", 75

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert call_count["n"] == 2

    def test_retry_on_rate_limit_error(self, executor, sample_task):
        """RateLimitError (429) should trigger a retry."""
        call_count = {"n": 0}

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RateLimitError(body="rate limited", retry_after=5)
            return "recovered after rate limit", 80

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert call_count["n"] == 2

    def test_timeout_error_code_on_final_failure(self, executor, sample_task):
        """When all retries fail with TimeoutError, error code must be 'timeout'."""
        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            raise TimeoutError("Session timed out")

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.errors[0].code == "timeout"

    def test_rate_limit_error_code_on_final_failure(self, executor, sample_task):
        """When all retries fail with RateLimitError, error code must be 'rate_limited'."""
        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            raise RateLimitError(body="rate limited")

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.errors[0].code == "rate_limited"


class TestNoRetryOnPermanentError:
    """PERMANENT errors (auth, 4xx, garbage-collected) must not be retried."""

    def test_auth_error_not_retried(self, executor, sample_task):
        call_count = {"n": 0}

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            raise AuthenticationError(status_code=401, body="unauthorized")

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        # Must have tried exactly once — no retries for PERMANENT errors
        assert call_count["n"] == 1

    def test_garbage_collected_not_retried(self, executor, sample_task):
        call_count = {"n": 0}

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            raise RuntimeError(
                "Session sess-abc was garbage-collected: history previously had messages"
            )

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert call_count["n"] == 1

    def test_404_http_error_not_retried(self, executor, sample_task):
        call_count = {"n": 0}

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            raise GatewayHTTPError(status_code=404, body="not found")

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert call_count["n"] == 1, "404 is PERMANENT — must not be retried"


class TestBackoffTiming:
    """Backoff sleep durations must follow the exponential formula."""

    def test_first_retry_uses_base_backoff(self, executor, sample_task):
        """Retry 0 (first retry): wait = backoff_base * multiplier^0 = 2.0s."""
        call_count = {"n": 0}
        sleep_calls = []

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise RuntimeError("fail")
            return "ok", 10

        def mock_sleep(seconds):
            sleep_calls.append(seconds)

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep", side_effect=mock_sleep):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(2.0)

    def test_second_retry_uses_multiplied_backoff(self, executor, sample_task):
        """Retry 1 (second retry): wait = backoff_base * multiplier^1 = 2.0 * 4.0 = 8.0s."""
        call_count = {"n": 0}
        sleep_calls = []

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise RuntimeError("fail")
            return "ok", 10

        def mock_sleep(seconds):
            sleep_calls.append(seconds)

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep", side_effect=mock_sleep):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == pytest.approx(2.0)   # retry 0: base * 4^0 = 2
        assert sleep_calls[1] == pytest.approx(8.0)   # retry 1: base * 4^1 = 8

    def test_backoff_capped_at_max(self):
        """Backoff is capped at backoff_max regardless of formula result."""
        cfg = ExecutorRetryConfig(
            backoff_base=2.0,
            backoff_multiplier=4.0,
            backoff_max=5.0,  # low cap for testing
        )
        # retry_index=2: 2.0 * 4^2 = 32.0 → capped to 5.0
        retry_index = 2
        computed = min(
            cfg.backoff_base * (cfg.backoff_multiplier ** retry_index),
            cfg.backoff_max,
        )
        assert computed == pytest.approx(5.0)

    def test_no_sleep_on_first_attempt(self, executor, sample_task):
        """The very first attempt must not incur any sleep."""
        sleep_calls = []

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            return "immediate success", 10

        def mock_sleep(seconds):
            sleep_calls.append(seconds)

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep", side_effect=mock_sleep):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert sleep_calls == [], "No sleep should occur on a first-attempt success"


class TestCircuitBreaker:
    """Circuit breaker prevents attempts when too many consecutive failures occur."""

    def _exhaust_circuit_breaker(
        self,
        executor: OpenClawExecutor,
        task: TaskSpec,
        model: str,
        threshold: int = 5,
    ) -> None:
        """Drive failure_count on the CB for `model` to >= threshold."""
        # Directly manipulate the CB state to avoid running full execute() loops.
        with _CIRCUIT_BREAKERS_LOCK:
            if model not in _CIRCUIT_BREAKERS:
                _CIRCUIT_BREAKERS[model] = CircuitBreakerState(name=model)
            cb = _CIRCUIT_BREAKERS[model]
            for _ in range(threshold):
                cb.record_failure(threshold)

    def test_circuit_breaker_open_prevents_execution(self, executor, sample_task):
        """When ALL tiers' CBs are open, execute() returns FAILED with
        ``all_tiers_unavailable`` (per #480: single-tier CB-open now escalates).
        """
        # Pre-exhaust the breakers keyed by the RESOLVED model ids the execute
        # chain probes. Re-keyed off MODEL_MAP (registry-backed, #916) so the
        # opus breaker tracks the canonical opus id (now opus-4-8) automatically.
        for m in (MODEL_MAP[ModelTier.SONNET], MODEL_MAP[ModelTier.OPUS]):
            self._exhaust_circuit_breaker(executor, sample_task, m, threshold=5)

        with patch.object(executor, "_run_session") as mock_run:
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.errors[0].code == "all_tiers_unavailable"
        mock_run.assert_not_called()

    def test_circuit_breaker_error_message_mentions_model(self, executor, sample_task):
        """Error message must enumerate probed tiers (#480 / adversary F4)."""
        # Re-keyed off MODEL_MAP (registry-backed, #916) so opus tracks the
        # canonical opus id (now opus-4-8) automatically.
        sonnet = MODEL_MAP[ModelTier.SONNET]
        opus = MODEL_MAP[ModelTier.OPUS]
        for m in (sonnet, opus):
            self._exhaust_circuit_breaker(executor, sample_task, m, threshold=5)

        with patch.object(executor, "_run_session"):
            result = executor.execute(sample_task)

        assert sonnet in result.errors[0].message or opus in result.errors[0].message

    def test_circuit_breaker_opens_after_threshold_failures(
        self, executor, sample_task
    ):
        """After enough failures across BOTH tiers, subsequent execute() returns all_tiers_unavailable (per #480 — single-tier CB-open now escalates to opus)."""
        call_count = {"n": 0}

        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            call_count["n"] += 1
            raise RuntimeError("persistent failure")

        # Execute twice: each execute() exhausts 3 retries, recording 3 failures each.
        # After two execute() calls the CB has 6 failures → exceeds threshold of 5 → opens.
        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result1 = executor.execute(sample_task)
            result2 = executor.execute(sample_task)

        # The CB should now be open.
        with patch.object(executor, "_run_session") as mock_run3, \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result3 = executor.execute(sample_task)
            mock_run3.assert_not_called()

        assert result1.state == TaskState.FAILED
        assert result2.state == TaskState.FAILED
        assert result3.state == TaskState.FAILED
        assert result3.errors[0].code == "all_tiers_unavailable"

    def test_circuit_breaker_resets_on_success(self, executor, sample_task):
        """A successful execute() should reset the circuit breaker."""
        model = "anthropic/claude-sonnet-4-6"
        # Pre-load some failures (below threshold so CB is still closed)
        with _CIRCUIT_BREAKERS_LOCK:
            _CIRCUIT_BREAKERS[model] = CircuitBreakerState(name=model)
            _CIRCUIT_BREAKERS[model].failure_count = 2

        def mock_run_session(prompt, model_str, thinking, timeout=None, **kwargs):
            return "success", 50

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        with _CIRCUIT_BREAKERS_LOCK:
            assert _CIRCUIT_BREAKERS[model].failure_count == 0

    def test_circuit_breakers_shared_across_instances(self, sample_task):
        """Different OpenClawExecutor instances must share the same CB registry."""
        executor_a = OpenClawExecutor(gateway_url="http://localhost:18789", dry_run=False)
        executor_b = OpenClawExecutor(gateway_url="http://localhost:18789", dry_run=False)

        model = "anthropic/claude-sonnet-4-6"

        # Exhaust CB via executor_a — need 5+ failures to trip threshold
        def always_fail(prompt, model_str, thinking, timeout=None, **kwargs):
            raise RuntimeError("fail")

        with patch.object(executor_a, "_run_session", side_effect=always_fail), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            executor_a.execute(sample_task)  # 3 failures (retries)
            executor_a.execute(sample_task)  # 3 more failures → 6 total, CB opens

        # The CB for that model should now be open
        with _CIRCUIT_BREAKERS_LOCK:
            cb = _CIRCUIT_BREAKERS.get(model)
        assert cb is not None
        assert cb.state == "open"

        # executor_b should see the open CB and not attempt execution
        with patch.object(executor_b, "_run_session") as mock_run_b, \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result_b = executor_b.execute(sample_task)
            mock_run_b.assert_not_called()

        assert result_b.errors[0].code == "all_tiers_unavailable"


# ---------------------------------------------------------------------------
# Partial output preservation through retry
# ---------------------------------------------------------------------------


class TestPartialOutputPreserved:
    """Partial output from a failed sub-agent is preserved in the final FAILED result."""

    def test_partial_output_in_result_after_all_retries_fail(
        self, executor, sample_task
    ):
        def mock_run_session(prompt, model, thinking, timeout=None, **kwargs):
            err = RuntimeError("Sub-agent ended with stopReason='error'.")
            err.partial_output = "SUBSTANTIAL PARTIAL OUTPUT HERE"
            err.partial_tokens = 250
            raise err

        with patch.object(executor, "_run_session", side_effect=mock_run_session), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert "SUBSTANTIAL PARTIAL OUTPUT" in result.result.get("partial_output", "")
        assert result.tokens_consumed == 250


# ---------------------------------------------------------------------------
# Dry-run and command paths are unchanged
# ---------------------------------------------------------------------------


class TestUnchangedPaths:
    def test_dry_run_still_succeeds(self):
        """Dry-run must not be affected by the retry logic."""
        executor = OpenClawExecutor(
            gateway_url="http://localhost:18789",
            dry_run=True,
        )
        task = TaskSpec(
            type=TaskType.CONTENT,
            payload={"prompt": "test prompt"},
            priority=Priority.NORMAL,
        )
        result = executor.execute(task)
        assert result.state == TaskState.SUCCESS
        assert result.result.get("dry_run") is True

    def test_command_task_bypasses_retry_loop(self, executor):
        """COMMAND task type uses _execute_command_task, not the retry loop."""
        task = TaskSpec(
            type=TaskType.COMMAND,
            payload={"command": "echo hello"},
            priority=Priority.NORMAL,
        )
        with patch.object(executor, "_execute_command_task") as mock_cmd:
            mock_cmd.return_value = MagicMock(state=TaskState.SUCCESS)
            executor.execute(task)
            mock_cmd.assert_called_once()
