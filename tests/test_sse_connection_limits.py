"""Tests for #841 — SSE connection limits + per-IP rate cap.

Without limits, a malicious or buggy client can hold thousands of SSE
connections open and exhaust server memory + file descriptors. The
limiter is module-scope (`web.api._SSE_LIMITER`) so tests can drive
admit/release directly without opening real streams (which TestClient
blocks on indefinitely due to the 1-second poll loop in the stream
generator).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def limiter():
    """Reset the module-scope limiter so each test starts from zero."""
    from orchestration_engine.web.api import _SSE_LIMITER
    _SSE_LIMITER._reset_for_tests()
    yield _SSE_LIMITER
    _SSE_LIMITER._reset_for_tests()


@pytest.fixture
def client(tmp_path, monkeypatch, limiter):
    monkeypatch.setenv("ORCH_DB_PATH", str(tmp_path / "engine.db"))
    from orchestration_engine.web.api import create_api_app
    return TestClient(create_api_app(db_path=str(tmp_path / "engine.db")))


# ---------------------------------------------------------------------------
# Limiter unit tests (direct admit/release)
# ---------------------------------------------------------------------------


class TestLimits:
    def test_defaults_are_100_and_10(self, monkeypatch):
        monkeypatch.delenv("ORCH_SSE_MAX_TOTAL", raising=False)
        monkeypatch.delenv("ORCH_SSE_MAX_PER_IP", raising=False)
        from orchestration_engine.web.api import _SseConnectionLimiter
        assert _SseConnectionLimiter.limits() == (100, 10)

    def test_env_vars_honoured(self, monkeypatch):
        monkeypatch.setenv("ORCH_SSE_MAX_TOTAL", "42")
        monkeypatch.setenv("ORCH_SSE_MAX_PER_IP", "7")
        from orchestration_engine.web.api import _SseConnectionLimiter
        assert _SseConnectionLimiter.limits() == (42, 7)

    def test_malformed_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ORCH_SSE_MAX_TOTAL", "not-a-number")
        monkeypatch.setenv("ORCH_SSE_MAX_PER_IP", "")
        from orchestration_engine.web.api import _SseConnectionLimiter
        assert _SseConnectionLimiter.limits() == (100, 10)


class TestAdmitRelease:
    def test_admit_increments_counters(self, limiter):
        assert limiter.admit("10.0.0.1") is None
        m = limiter.metrics()
        assert m["active_total"] == 1
        assert m["active_per_ip"] == {"10.0.0.1": 1}

    def test_release_decrements_and_removes_per_ip_entry_at_zero(self, limiter):
        limiter.admit("10.0.0.1")
        limiter.release("10.0.0.1")
        m = limiter.metrics()
        assert m["active_total"] == 0
        assert m["active_per_ip"] == {}  # key removed at zero

    def test_release_saturates_at_zero(self, limiter):
        """Defensive: release() called without a preceding admit() does
        not crash and counts saturate at zero."""
        limiter.release("never_admitted")
        limiter.release("never_admitted")
        assert limiter.metrics()["active_total"] == 0

    def test_multiple_ips_tracked_independently(self, limiter):
        limiter.admit("10.0.0.1")
        limiter.admit("10.0.0.2")
        limiter.admit("10.0.0.2")
        m = limiter.metrics()
        assert m["active_total"] == 3
        assert m["active_per_ip"] == {"10.0.0.1": 1, "10.0.0.2": 2}


class TestPerIpCap:
    def test_admit_returns_error_when_per_ip_cap_hit(self, limiter, monkeypatch):
        monkeypatch.setenv("ORCH_SSE_MAX_PER_IP", "2")
        monkeypatch.setenv("ORCH_SSE_MAX_TOTAL", "100")
        assert limiter.admit("10.0.0.1") is None
        assert limiter.admit("10.0.0.1") is None
        err = limiter.admit("10.0.0.1")
        assert err is not None
        assert "per-IP" in err
        assert "10.0.0.1" in err
        # Counters did NOT advance past the cap
        assert limiter.metrics()["active_per_ip"]["10.0.0.1"] == 2

    def test_other_ips_unaffected_by_one_ip_at_cap(self, limiter, monkeypatch):
        monkeypatch.setenv("ORCH_SSE_MAX_PER_IP", "1")
        limiter.admit("10.0.0.1")
        assert limiter.admit("10.0.0.1") is not None  # cap hit
        assert limiter.admit("10.0.0.2") is None  # different IP — OK

    def test_release_frees_slot_for_same_ip(self, limiter, monkeypatch):
        monkeypatch.setenv("ORCH_SSE_MAX_PER_IP", "1")
        limiter.admit("10.0.0.1")
        assert limiter.admit("10.0.0.1") is not None
        limiter.release("10.0.0.1")
        assert limiter.admit("10.0.0.1") is None


class TestTotalCap:
    def test_total_cap_overrides_per_ip(self, limiter, monkeypatch):
        monkeypatch.setenv("ORCH_SSE_MAX_TOTAL", "2")
        monkeypatch.setenv("ORCH_SSE_MAX_PER_IP", "100")
        limiter.admit("10.0.0.1")
        limiter.admit("10.0.0.2")
        err = limiter.admit("10.0.0.3")
        assert err is not None
        assert "total" in err.lower()
        assert limiter.metrics()["active_total"] == 2


class TestCapDisabledByZero:
    def test_zero_per_ip_disables_per_ip_check(self, limiter, monkeypatch):
        monkeypatch.setenv("ORCH_SSE_MAX_PER_IP", "0")
        monkeypatch.setenv("ORCH_SSE_MAX_TOTAL", "100")
        for _ in range(50):
            assert limiter.admit("10.0.0.1") is None
        assert limiter.metrics()["active_per_ip"]["10.0.0.1"] == 50

    def test_zero_total_disables_total_check(self, limiter, monkeypatch):
        monkeypatch.setenv("ORCH_SSE_MAX_TOTAL", "0")
        monkeypatch.setenv("ORCH_SSE_MAX_PER_IP", "100")
        # Bump total beyond what would be the normal cap of 100
        for i in range(150):
            assert limiter.admit(f"10.0.{i // 100}.{i % 100}") is None
        assert limiter.metrics()["active_total"] == 150


# ---------------------------------------------------------------------------
# /api/v1/sse/metrics endpoint
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    def test_endpoint_returns_zero_when_no_streams(self, client):
        body = client.get("/api/v1/sse/metrics").json()
        assert body["active_total"] == 0
        assert body["active_per_ip"] == {}

    def test_endpoint_reflects_admits(self, client, limiter):
        limiter.admit("10.0.0.1")
        limiter.admit("10.0.0.2")
        body = client.get("/api/v1/sse/metrics").json()
        assert body["active_total"] == 2
        assert body["active_per_ip"] == {"10.0.0.1": 1, "10.0.0.2": 1}

    def test_endpoint_reflects_env_limits(self, client, monkeypatch):
        monkeypatch.setenv("ORCH_SSE_MAX_TOTAL", "42")
        monkeypatch.setenv("ORCH_SSE_MAX_PER_IP", "7")
        body = client.get("/api/v1/sse/metrics").json()
        assert body["max_total"] == 42
        assert body["max_per_ip"] == 7


# ---------------------------------------------------------------------------
# Source-wiring guards — prevent silent regressions
# ---------------------------------------------------------------------------


class TestSourceWiring:
    @pytest.fixture(autouse=True)
    def _src(self):
        self.src = (
            Path(__file__).resolve().parent.parent
            / "src" / "orchestration_engine" / "web" / "api.py"
        ).read_text()

    def test_stream_endpoint_calls_admit(self):
        assert "_sse_limiter.admit(client_ip)" in self.src, (
            "stream endpoint no longer calls _sse_limiter.admit — limit bypassed."
        )

    def test_stream_endpoint_calls_release_in_finally(self):
        # finally + _sse_limiter.release must both appear; their pairing
        # is what guarantees counters don't leak when the stream ends.
        assert "_sse_limiter.release(client_ip)" in self.src
        assert "finally:" in self.src

    def test_env_var_names_referenced(self):
        assert "ORCH_SSE_MAX_TOTAL" in self.src
        assert "ORCH_SSE_MAX_PER_IP" in self.src

    def test_retry_after_header_emitted(self):
        assert '"Retry-After": "30"' in self.src or "'Retry-After': '30'" in self.src
