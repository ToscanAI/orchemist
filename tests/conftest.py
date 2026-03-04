"""Shared pytest fixtures and test configuration.

Provides session-scoped and autouse fixtures that handle global state cleanup
between test cases (e.g. the module-level circuit-breaker registry introduced
in issue #346).
"""

import pytest


@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    """Reset the OpenClawExecutor module-level circuit-breaker registry before each test.

    The ``_CIRCUIT_BREAKERS`` dict in ``openclaw_executor`` is intentionally
    shared across all executor instances in a process (issue #346).  Without
    this fixture, failures in one test would open a circuit breaker and cause
    the next test to receive a ``circuit_open`` result instead of executing its
    intended code path.
    """
    from orchestration_engine.openclaw_executor import (
        _CIRCUIT_BREAKERS,
        _CIRCUIT_BREAKERS_LOCK,
    )

    with _CIRCUIT_BREAKERS_LOCK:
        _CIRCUIT_BREAKERS.clear()

    yield

    # Post-test cleanup (belt-and-suspenders).
    with _CIRCUIT_BREAKERS_LOCK:
        _CIRCUIT_BREAKERS.clear()
