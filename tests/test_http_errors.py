"""Tests for structured HTTP error classification in openclaw_executor.py.

Covers issue #244: the custom exception hierarchy in errors.py and the
updated _http_post()/_http_get() methods that raise them.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.response
from http.client import HTTPMessage
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.errors import (
    AuthenticationError,
    GatewayHTTPError,
    GatewayUnavailableError,
    OrchestratorError,
    RateLimitError,
    classify_http_error,
)
from orchestration_engine.openclaw_executor import OpenClawExecutor
from orchestration_engine.schemas import (
    ModelTier,
    Priority,
    TaskSpec,
    TaskState,
    TaskType,
)


# ---------------------------------------------------------------------------
# Helper: build a fake urllib.error.HTTPError
# ---------------------------------------------------------------------------

def _make_http_error(
    status_code: int,
    body: str = "error body",
    headers: dict | None = None,
) -> urllib.error.HTTPError:
    """Return a urllib.error.HTTPError with the given code, body, and headers."""
    encoded = body.encode("utf-8")
    fp = io.BytesIO(encoded)

    # Build an HTTPMessage (email.message.Message subclass) from raw headers
    raw_headers: dict = headers or {}
    msg = HTTPMessage()
    for k, v in raw_headers.items():
        msg[k] = v

    return urllib.error.HTTPError(
        url="http://fake-gateway/test",
        code=status_code,
        msg=str(status_code),
        hdrs=msg,
        fp=fp,
    )


# ---------------------------------------------------------------------------
# classify_http_error factory tests
# ---------------------------------------------------------------------------

class TestClassifyHttpError:
    """classify_http_error must return the right subclass for every code."""

    def test_429_returns_rate_limit_error(self):
        exc = classify_http_error(429, "too many requests")
        assert isinstance(exc, RateLimitError)

    def test_429_is_gateway_http_error(self):
        exc = classify_http_error(429, "too many requests")
        assert isinstance(exc, GatewayHTTPError)

    def test_429_is_orchestrator_error(self):
        exc = classify_http_error(429, "too many requests")
        assert isinstance(exc, OrchestratorError)

    def test_429_extracts_retry_after_from_headers(self):
        headers = {"Retry-After": "30"}
        exc = classify_http_error(429, "rate limited", headers=headers)
        assert isinstance(exc, RateLimitError)
        assert exc.retry_after == 30

    def test_429_retry_after_none_when_header_absent(self):
        exc = classify_http_error(429, "rate limited")
        assert isinstance(exc, RateLimitError)
        assert exc.retry_after is None

    def test_429_retry_after_none_when_header_invalid(self):
        headers = {"Retry-After": "not-a-number"}
        exc = classify_http_error(429, "rate limited", headers=headers)
        assert isinstance(exc, RateLimitError)
        assert exc.retry_after is None

    def test_401_returns_authentication_error(self):
        exc = classify_http_error(401, "unauthorized")
        assert isinstance(exc, AuthenticationError)

    def test_403_returns_authentication_error(self):
        exc = classify_http_error(403, "forbidden")
        assert isinstance(exc, AuthenticationError)

    def test_401_is_gateway_http_error(self):
        exc = classify_http_error(401, "unauthorized")
        assert isinstance(exc, GatewayHTTPError)

    def test_503_returns_gateway_unavailable_error(self):
        exc = classify_http_error(503, "service unavailable")
        assert isinstance(exc, GatewayUnavailableError)

    def test_502_returns_gateway_unavailable_error(self):
        exc = classify_http_error(502, "bad gateway")
        assert isinstance(exc, GatewayUnavailableError)

    def test_504_returns_gateway_unavailable_error(self):
        exc = classify_http_error(504, "gateway timeout")
        assert isinstance(exc, GatewayUnavailableError)

    def test_503_is_gateway_http_error(self):
        exc = classify_http_error(503, "service unavailable")
        assert isinstance(exc, GatewayHTTPError)

    def test_404_returns_base_gateway_http_error(self):
        exc = classify_http_error(404, "not found")
        assert type(exc) is GatewayHTTPError

    def test_400_returns_base_gateway_http_error(self):
        exc = classify_http_error(400, "bad request")
        assert type(exc) is GatewayHTTPError

    def test_500_returns_base_gateway_http_error(self):
        exc = classify_http_error(500, "internal server error")
        assert type(exc) is GatewayHTTPError


# ---------------------------------------------------------------------------
# is_retryable property tests
# ---------------------------------------------------------------------------

class TestIsRetryable:
    """is_retryable must be True for 429/502/503/504 and False for 401/403/404."""

    @pytest.mark.parametrize("code", [429, 502, 503, 504])
    def test_retryable_codes(self, code: int):
        exc = classify_http_error(code, "body")
        assert exc.is_retryable is True, f"Expected is_retryable=True for {code}"

    @pytest.mark.parametrize("code", [401, 403, 404])
    def test_non_retryable_codes(self, code: int):
        exc = classify_http_error(code, "body")
        assert exc.is_retryable is False, f"Expected is_retryable=False for {code}"

    def test_rate_limit_error_is_retryable(self):
        exc = RateLimitError("too many requests")
        assert exc.is_retryable is True

    def test_authentication_error_not_retryable(self):
        exc = AuthenticationError(401, "unauthorized")
        assert exc.is_retryable is False

    def test_authentication_error_403_not_retryable(self):
        exc = AuthenticationError(403, "forbidden")
        assert exc.is_retryable is False

    def test_gateway_unavailable_error_is_retryable(self):
        exc = GatewayUnavailableError(503, "service unavailable")
        assert exc.is_retryable is True


# ---------------------------------------------------------------------------
# RateLimitError attributes
# ---------------------------------------------------------------------------

class TestRateLimitError:
    """RateLimitError must carry status_code=429 and optional retry_after."""

    def test_status_code_is_429(self):
        exc = RateLimitError("rate limited", retry_after=60)
        assert exc.status_code == 429

    def test_body_preserved(self):
        exc = RateLimitError("rate limited payload", retry_after=10)
        assert exc.body == "rate limited payload"

    def test_retry_after_integer(self):
        exc = RateLimitError("rate limited", retry_after=120)
        assert exc.retry_after == 120

    def test_retry_after_none_by_default(self):
        exc = RateLimitError("rate limited")
        assert exc.retry_after is None

    def test_str_includes_retry_after(self):
        exc = RateLimitError("rate limited", retry_after=45)
        assert "45" in str(exc)

    def test_str_without_retry_after(self):
        exc = RateLimitError("rate limited")
        assert "rate limit" in str(exc).lower()


# ---------------------------------------------------------------------------
# AuthenticationError attributes
# ---------------------------------------------------------------------------

class TestAuthenticationError:
    """AuthenticationError must only accept 401/403."""

    def test_401_accepted(self):
        exc = AuthenticationError(401, "unauthorized")
        assert exc.status_code == 401

    def test_403_accepted(self):
        exc = AuthenticationError(403, "forbidden")
        assert exc.status_code == 403

    def test_invalid_code_raises_value_error(self):
        with pytest.raises(ValueError, match="401 or 403"):
            AuthenticationError(500, "bad code")


# ---------------------------------------------------------------------------
# GatewayUnavailableError attributes
# ---------------------------------------------------------------------------

class TestGatewayUnavailableError:
    """GatewayUnavailableError must only accept 502/503/504."""

    @pytest.mark.parametrize("code", [502, 503, 504])
    def test_valid_codes_accepted(self, code: int):
        exc = GatewayUnavailableError(code, "unavailable")
        assert exc.status_code == code

    def test_invalid_code_raises_value_error(self):
        with pytest.raises(ValueError, match="502, 503 or 504"):
            GatewayUnavailableError(429, "bad code")


# ---------------------------------------------------------------------------
# OpenClawExecutor._http_post integration tests
# ---------------------------------------------------------------------------

class TestHttpPostRaisesStructuredErrors:
    """_http_post must raise the appropriate GatewayHTTPError subclass."""

    @pytest.fixture
    def executor(self):
        return OpenClawExecutor(
            gateway_url="http://localhost:18789",
            gateway_token="test-token",
        )

    def _patch_urlopen_error(self, status_code: int, body: str = "error", headers: dict | None = None):
        """Context manager that makes urlopen raise an HTTPError."""
        http_err = _make_http_error(status_code, body, headers)
        return patch("urllib.request.urlopen", side_effect=http_err)

    def test_post_429_raises_rate_limit_error(self, executor):
        with self._patch_urlopen_error(429, "rate limited"):
            with pytest.raises(RateLimitError) as exc_info:
                executor._http_post("http://localhost:18789/test", {})
            assert exc_info.value.status_code == 429

    def test_post_429_with_retry_after_header(self, executor):
        with self._patch_urlopen_error(429, "rate limited", {"Retry-After": "60"}):
            with pytest.raises(RateLimitError) as exc_info:
                executor._http_post("http://localhost:18789/test", {})
            assert exc_info.value.retry_after == 60

    def test_post_401_raises_authentication_error(self, executor):
        with self._patch_urlopen_error(401, "unauthorized"):
            with pytest.raises(AuthenticationError) as exc_info:
                executor._http_post("http://localhost:18789/test", {})
            assert exc_info.value.status_code == 401

    def test_post_403_raises_authentication_error(self, executor):
        with self._patch_urlopen_error(403, "forbidden"):
            with pytest.raises(AuthenticationError):
                executor._http_post("http://localhost:18789/test", {})

    def test_post_503_raises_gateway_unavailable_error(self, executor):
        with self._patch_urlopen_error(503, "service unavailable"):
            with pytest.raises(GatewayUnavailableError) as exc_info:
                executor._http_post("http://localhost:18789/test", {})
            assert exc_info.value.status_code == 503

    def test_post_502_raises_gateway_unavailable_error(self, executor):
        with self._patch_urlopen_error(502, "bad gateway"):
            with pytest.raises(GatewayUnavailableError):
                executor._http_post("http://localhost:18789/test", {})

    def test_post_504_raises_gateway_unavailable_error(self, executor):
        with self._patch_urlopen_error(504, "gateway timeout"):
            with pytest.raises(GatewayUnavailableError):
                executor._http_post("http://localhost:18789/test", {})

    def test_post_404_raises_base_gateway_http_error(self, executor):
        with self._patch_urlopen_error(404, "not found"):
            with pytest.raises(GatewayHTTPError) as exc_info:
                executor._http_post("http://localhost:18789/test", {})
            # Must be the base class, not a subclass
            assert type(exc_info.value) is GatewayHTTPError

    def test_post_500_raises_base_gateway_http_error(self, executor):
        with self._patch_urlopen_error(500, "internal error"):
            with pytest.raises(GatewayHTTPError):
                executor._http_post("http://localhost:18789/test", {})

    def test_all_gateway_errors_are_subclass_of_orchestrator_error(self, executor):
        with self._patch_urlopen_error(503, "unavailable"):
            with pytest.raises(OrchestratorError):
                executor._http_post("http://localhost:18789/test", {})


# ---------------------------------------------------------------------------
# OpenClawExecutor._http_get integration tests
# ---------------------------------------------------------------------------

class TestHttpGetRaisesStructuredErrors:
    """_http_get must raise the appropriate GatewayHTTPError subclass."""

    @pytest.fixture
    def executor(self):
        return OpenClawExecutor(
            gateway_url="http://localhost:18789",
            gateway_token="test-token",
        )

    def _patch_urlopen_error(self, status_code: int, body: str = "error", headers: dict | None = None):
        http_err = _make_http_error(status_code, body, headers)
        return patch("urllib.request.urlopen", side_effect=http_err)

    def test_get_429_raises_rate_limit_error(self, executor):
        with self._patch_urlopen_error(429):
            with pytest.raises(RateLimitError):
                executor._http_get("http://localhost:18789/api/sessions/abc")

    def test_get_401_raises_authentication_error(self, executor):
        with self._patch_urlopen_error(401):
            with pytest.raises(AuthenticationError):
                executor._http_get("http://localhost:18789/api/sessions/abc")

    def test_get_503_raises_gateway_unavailable_error(self, executor):
        with self._patch_urlopen_error(503):
            with pytest.raises(GatewayUnavailableError):
                executor._http_get("http://localhost:18789/api/sessions/abc")


# ---------------------------------------------------------------------------
# execute() method: RateLimitError handling
# ---------------------------------------------------------------------------

class TestExecuteHandlesRateLimitError:
    """execute() must catch RateLimitError and log clearly, returning FAILED state."""

    @pytest.fixture
    def executor(self):
        return OpenClawExecutor(
            gateway_url="http://localhost:18789",
            gateway_token="test-token",
        )

    @pytest.fixture
    def task(self):
        return TaskSpec(
            type=TaskType.CONTENT,
            payload={"prompt": "Write a haiku about rate limits."},
            priority=Priority.NORMAL,
        )

    def _make_rate_limit_http_error(self, retry_after: int | None = None) -> urllib.error.HTTPError:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        return _make_http_error(429, "rate limited", headers)

    def test_execute_rate_limited_returns_failed_state(self, executor, task):
        http_err = self._make_rate_limit_http_error(retry_after=30)
        # Mock time.sleep to avoid real backoff delays during retries (#346).
        with patch("urllib.request.urlopen", side_effect=http_err), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(task)
        assert result.state == TaskState.FAILED

    def test_execute_rate_limited_error_code_is_rate_limited(self, executor, task):
        http_err = self._make_rate_limit_http_error()
        # Mock time.sleep to avoid real backoff delays during retries (#346).
        with patch("urllib.request.urlopen", side_effect=http_err), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(task)
        assert len(result.errors) == 1
        assert result.errors[0].code == "rate_limited"

    def test_execute_rate_limited_logs_warning_with_retry_after(self, executor, task, caplog):
        import logging
        http_err = self._make_rate_limit_http_error(retry_after=60)
        # Mock time.sleep to avoid real backoff delays during retries (#346).
        with patch("urllib.request.urlopen", side_effect=http_err), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            with caplog.at_level(logging.WARNING):
                executor.execute(task)
        # Should contain "429" and "retry after 60s" (logged once per attempt)
        combined = " ".join(caplog.messages)
        assert "429" in combined
        assert "60" in combined

    def test_execute_rate_limited_logs_warning_without_retry_after(self, executor, task, caplog):
        import logging
        http_err = self._make_rate_limit_http_error(retry_after=None)
        # Mock time.sleep to avoid real backoff delays during retries (#346).
        with patch("urllib.request.urlopen", side_effect=http_err), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            with caplog.at_level(logging.WARNING):
                executor.execute(task)
        combined = " ".join(caplog.messages)
        assert "429" in combined

    def test_execute_auth_error_uses_generic_handler(self, executor, task):
        """AuthenticationError (PERMANENT) must not be retried; returns execution_error."""
        http_err = _make_http_error(401, "unauthorized")
        # No time.sleep mock needed — PERMANENT errors are not retried (#346).
        with patch("urllib.request.urlopen", side_effect=http_err):
            result = executor.execute(task)
        assert result.state == TaskState.FAILED
        # Generic handler sets code="execution_error"
        assert result.errors[0].code == "execution_error"

    def test_execute_503_uses_generic_handler(self, executor, task):
        """GatewayUnavailableError (TRANSIENT) retries then falls to generic handler."""
        http_err = _make_http_error(503, "service unavailable")
        # Mock time.sleep to avoid real backoff delays during retries (#346).
        with patch("urllib.request.urlopen", side_effect=http_err), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(task)
        assert result.state == TaskState.FAILED
        assert result.errors[0].code == "execution_error"
