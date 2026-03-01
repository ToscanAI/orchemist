"""Custom exception hierarchy for the orchestration engine.

Provides structured HTTP error types so callers can distinguish retryable
errors (rate limits, gateway unavailability) from fatal errors (auth failures,
bad requests) without inspecting raw error strings.

Usage::

    from orchestration_engine.errors import (
        GatewayHTTPError,
        RateLimitError,
        AuthenticationError,
        GatewayUnavailableError,
    )

    try:
        result = executor._http_post(url, body)
    except RateLimitError as exc:
        print(f"Rate limited — retry after {exc.retry_after}s")
    except GatewayUnavailableError:
        print("Gateway is down, retry later")
    except AuthenticationError:
        print("Check your API token")
    except GatewayHTTPError as exc:
        print(f"HTTP {exc.status_code}: {exc.body}")
"""

from __future__ import annotations

from typing import Optional


class OrchestratorError(Exception):
    """Base exception for all orchestration engine errors."""


class ValidationError(OrchestratorError):
    """Raised when a pipeline template fails structural validation."""


class GatewayHTTPError(OrchestratorError):
    """Raised when the OpenClaw gateway returns an HTTP error response.

    Attributes:
        status_code:  The HTTP status code (e.g. 400, 404, 503).
        body:         The response body as a string.
        is_retryable: Whether this error class is considered retryable.
                      True for 429, 502, 503, 504; False for 4xx auth/client errors.
    """

    _RETRYABLE_CODES = frozenset({429, 502, 503, 504})

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Gateway HTTP error {status_code}: {body}")

    @property
    def is_retryable(self) -> bool:
        """Return True when the error may resolve on retry."""
        return self.status_code in self._RETRYABLE_CODES


class RateLimitError(GatewayHTTPError):
    """Raised on HTTP 429 Too Many Requests.

    Includes the ``Retry-After`` value from the response headers when present.

    Attributes:
        retry_after: Seconds to wait before retrying, or ``None`` if the header
                     was absent or unparseable.
    """

    def __init__(self, body: str, retry_after: Optional[int] = None) -> None:
        self.retry_after = retry_after
        super().__init__(status_code=429, body=body)
        # Override the default message to surface retry_after
        if retry_after is not None:
            self.args = (
                f"Gateway HTTP error 429 (rate limit) — retry after {retry_after}s: {body}",
            )
        else:
            self.args = (f"Gateway HTTP error 429 (rate limit): {body}",)


class AuthenticationError(GatewayHTTPError):
    """Raised on HTTP 401 Unauthorized or 403 Forbidden.

    This error is *not* retryable — the caller must fix credentials.
    """

    def __init__(self, status_code: int, body: str) -> None:
        if status_code not in (401, 403):
            raise ValueError(
                f"AuthenticationError expects status 401 or 403, got {status_code}"
            )
        super().__init__(status_code=status_code, body=body)


class GatewayUnavailableError(GatewayHTTPError):
    """Raised on HTTP 502, 503, or 504 when the gateway is temporarily unavailable.

    This error *is* retryable.
    """

    def __init__(self, status_code: int, body: str) -> None:
        if status_code not in (502, 503, 504):
            raise ValueError(
                f"GatewayUnavailableError expects status 502, 503 or 504, got {status_code}"
            )
        super().__init__(status_code=status_code, body=body)


def classify_http_error(status_code: int, body: str, headers=None) -> GatewayHTTPError:
    """Factory — return the most specific ``GatewayHTTPError`` subclass.

    Args:
        status_code: HTTP response status code.
        body:        Response body text.
        headers:     Optional ``http.client.HTTPMessage`` (or any mapping)
                     for extracting ``Retry-After`` headers on 429 responses.

    Returns:
        The appropriate :class:`GatewayHTTPError` subclass instance.
    """
    if status_code == 429:
        retry_after: Optional[int] = None
        if headers is not None:
            raw = None
            # Support both dict-like and http.client.HTTPMessage
            try:
                raw = headers.get("Retry-After") or headers.get("retry-after")
            except Exception:
                pass
            if raw is not None:
                try:
                    retry_after = int(raw)
                except (ValueError, TypeError):
                    pass
        return RateLimitError(body=body, retry_after=retry_after)

    if status_code in (401, 403):
        return AuthenticationError(status_code=status_code, body=body)

    if status_code in (502, 503, 504):
        return GatewayUnavailableError(status_code=status_code, body=body)

    return GatewayHTTPError(status_code=status_code, body=body)
