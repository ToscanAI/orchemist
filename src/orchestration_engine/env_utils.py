"""Shared environment-variable parsing helpers (Issue #865).

Canonical home for env-var-with-fallback idioms that were duplicated across
:mod:`notifications` (``_int`` nested inside ``NotificationDispatcher.from_env``)
and :mod:`web.api` (three ad-hoc ``try/except ValueError`` blocks for
``ORCH_SSE_MAX_TOTAL``, ``ORCH_SSE_MAX_PER_IP``, and ``ORCH_MAX_DAEMONS``).

The canonical helper is :func:`env_int`, byte-for-byte equivalent to the
``_int`` previously defined inside ``NotificationDispatcher.from_env``.
``None`` (env var unset) and malformed values both fall back to *default*
silently — operators can mistype the env var without crashing the server.
"""

from __future__ import annotations


__all__ = ["env_int"]


def env_int(val: str | None, default: int) -> int:
    """Parse *val* as an int, falling back to *default* on None or malformed input.

    Mirrors the deprecated ``_int`` helper previously embedded inside
    ``NotificationDispatcher.from_env``.  Returns *default* unchanged when:
      - *val* is ``None`` (env var unset);
      - *val* is the empty string;
      - *val* cannot be parsed as an integer (e.g. ``"abc"``, ``"1.5"``).

    Typical call pattern::

        max_total = env_int(os.environ.get("ORCH_SSE_MAX_TOTAL"), 100)
    """
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default
