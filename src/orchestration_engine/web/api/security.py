"""Webhook security + rate-limiting helpers for the REST API (Issue #942, sub-issue 952a).

Closure-free, module-level helpers extracted verbatim from ``web/api.py`` as
part of the facade-preserving decomposition of the god-module.

Re-exported by ``web/api/__init__.py`` so the historical import path
``from orchestration_engine.web.api import _verify_github_signature`` keeps
resolving.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def _verify_github_signature(secret: str, payload_bytes: bytes, sig_header: Optional[str]) -> bool:
    """Verify an HMAC-SHA256 GitHub-style webhook signature.

    GitHub sends ``X-Hub-Signature-256: sha256=<hex_digest>``.  This function
    recomputes the HMAC-SHA256 of *payload_bytes* using *secret* and compares
    it to the digest in *sig_header* using a constant-time comparison to
    prevent timing attacks.

    Args:
        secret: Shared HMAC secret string.
        payload_bytes: Raw request body bytes.
        sig_header: Value of the ``X-Hub-Signature-256`` header
            (e.g. ``"sha256=abc123..."``).  May be ``None``.

    Returns:
        ``True`` when the signature is valid, ``False`` otherwise
        (including when *sig_header* is ``None`` or malformed).
    """
    if not sig_header:
        return False
    if not sig_header.startswith("sha256="):
        return False
    expected = sig_header[len("sha256=") :]
    computed = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, expected)


def _check_rate_limit(trigger_id: str, rate_limit: int, db: Any) -> bool:
    """Check whether a webhook trigger has exceeded its per-minute rate limit.

    Args:
        trigger_id: The trigger identifier to check.
        rate_limit: Maximum allowed invocations per minute (0 = unlimited).
        db: :class:`~orchestration_engine.db.Database` instance.

    Returns:
        ``True`` when the rate limit is exceeded (caller should return 429).
        ``False`` when the invocation is allowed.
    """
    if rate_limit == 0:
        return False  # Unlimited
    since = datetime.now(timezone.utc) - timedelta(seconds=60)
    count = db.count_webhook_invocations_since(trigger_id, since)
    return count >= rate_limit


class SlidingWindowRateLimiter:
    """A named, unit-testable sliding-window rate limiter for webhook triggers.

    Uses a 60-second sliding window backed by the ``webhook_invocations``
    table in the DB.  Delegates to the same logic as ``_check_rate_limit()``
    but wraps it in a proper class so it can be tested and extended
    independently.

    Example::

        limiter = SlidingWindowRateLimiter(db)
        if limiter.check(trigger_id, rate_limit):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    """

    def __init__(self, db: Any) -> None:
        """Initialise with a DB instance.

        Args:
            db: :class:`~orchestration_engine.db.Database` instance used to
                query invocation counts and record new invocations.
        """
        self._db = db

    def check(self, trigger_id: str, rate_limit: int) -> bool:
        """Return ``True`` if the rate limit is exceeded, ``False`` otherwise.

        Args:
            trigger_id: The trigger identifier to check.
            rate_limit: Maximum allowed invocations per minute (0 = unlimited).

        Returns:
            ``True`` when the caller should block the request (429).
            ``False`` when the invocation is allowed.
        """
        return _check_rate_limit(trigger_id, rate_limit, self._db)
