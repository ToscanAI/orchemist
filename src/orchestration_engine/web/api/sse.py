"""SSE connection limiting for the REST API (Issue #942, sub-issue 952a).

The per-process SSE connection limiter + its module-level singleton, extracted
verbatim from ``web/api.py`` as part of the facade-preserving decomposition of
the god-module.

Re-exported by ``web/api/__init__.py`` so the historical import paths
``from orchestration_engine.web.api import _SseConnectionLimiter`` and
``... import _SSE_LIMITER`` keep resolving (and keep referring to the SAME
process-wide singleton).
"""

import os
from typing import Any, Dict, Optional, Tuple

from orchestration_engine.env_utils import env_int


class _SseConnectionLimiter:
    """Per-process SSE connection counter + per-IP cap (Issue #841).

    Module-level singleton (``_SSE_LIMITER``) so that:
      - all FastAPI workers in the same process share counts
      - tests can import and drive admit/release directly without
        opening real streams (which TestClient blocks on indefinitely
        due to the 1-second poll loop)
      - the metrics endpoint and the stream endpoint read the same
        counters atomically

    Limits are env-var driven (``ORCH_SSE_MAX_TOTAL`` default 100,
    ``ORCH_SSE_MAX_PER_IP`` default 10; ``0`` disables the
    corresponding limit). Env vars are re-read on every ``admit`` call
    so operators can re-tune live without restarting.
    """

    def __init__(self) -> None:
        import threading  # noqa: PLC0415

        self._lock = threading.Lock()
        self._active_total: int = 0
        self._active_per_ip: Dict[str, int] = {}

    @staticmethod
    def limits() -> Tuple[int, int]:
        """Return ``(max_total, max_per_ip)`` from env vars. Malformed
        values fall back to the documented defaults — never raises."""
        max_total = env_int(os.environ.get("ORCH_SSE_MAX_TOTAL"), 100)
        max_per_ip = env_int(os.environ.get("ORCH_SSE_MAX_PER_IP"), 10)
        return max_total, max_per_ip

    def admit(self, client_ip: str) -> Optional[str]:
        """Try to admit a new SSE connection. Returns ``None`` on
        success (counters incremented) or a human-readable detail
        string when a limit is exceeded (counters unchanged).

        Caller MUST call :meth:`release` with the SAME ``client_ip``
        from a finally block when the connection ends.
        """
        max_total, max_per_ip = self.limits()
        with self._lock:
            if max_total > 0 and self._active_total >= max_total:
                return (
                    f"SSE total connection limit reached "
                    f"({self._active_total}/{max_total}). Try again later."
                )
            if max_per_ip > 0:
                cur = self._active_per_ip.get(client_ip, 0)
                if cur >= max_per_ip:
                    return (
                        f"SSE per-IP connection limit reached "
                        f"({cur}/{max_per_ip} from {client_ip}). "
                        f"Close one before opening another."
                    )
            self._active_total += 1
            self._active_per_ip[client_ip] = self._active_per_ip.get(client_ip, 0) + 1
        return None

    def release(self, client_ip: str) -> None:
        """Decrement counters for a closing connection. Saturating at
        zero — never goes negative even if release() is called more
        times than admit() (defensive)."""
        with self._lock:
            self._active_total = max(0, self._active_total - 1)
            cur = self._active_per_ip.get(client_ip, 0)
            if cur <= 1:
                self._active_per_ip.pop(client_ip, None)
            else:
                self._active_per_ip[client_ip] = cur - 1

    def metrics(self) -> Dict[str, Any]:
        """Return a snapshot dict suitable for JSON serialisation."""
        max_total, max_per_ip = self.limits()
        with self._lock:
            return {
                "active_total": self._active_total,
                "active_per_ip": dict(self._active_per_ip),
                "max_total": max_total,
                "max_per_ip": max_per_ip,
            }

    def _reset_for_tests(self) -> None:
        """Used by the test suite to start each test from zero counts."""
        with self._lock:
            self._active_total = 0
            self._active_per_ip.clear()


# Process-wide singleton. The web app injects this into request handlers
# via a closure reference; tests import it directly to verify counters.
_SSE_LIMITER = _SseConnectionLimiter()
