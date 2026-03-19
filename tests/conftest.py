"""Shared pytest fixtures and test configuration.

Provides session-scoped and autouse fixtures that handle global state cleanup
between test cases (e.g. the module-level circuit-breaker registry introduced
in issue #346).
"""

import os
from pathlib import Path

import pytest

# Repository root resolved from this conftest location.
_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def examples_on_path(monkeypatch):
    """Add examples/ to ORCH_TEMPLATES_PATH so fixture templates are resolvable.

    Use this in test methods that invoke CLI or webhook with 'coding-pipeline-fixture'
    or any other template that lives in examples/ rather than templates/.

    Issue #632: tests must not depend on production templates in templates/ by
    filesystem path. Stable fixtures live in examples/.
    """
    existing = os.environ.get("ORCH_TEMPLATES_PATH", "")
    examples = str(_REPO_ROOT / "examples")
    new_val = f"{examples}:{existing}" if existing else examples
    monkeypatch.setenv("ORCH_TEMPLATES_PATH", new_val)


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


# ---------------------------------------------------------------------------
# Issue #501: Shared fixtures for E2E integration tests
# ---------------------------------------------------------------------------

import json
from pathlib import Path
from unittest.mock import MagicMock

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def minimal_e2e_template() -> Path:
    """Return path to tests/fixtures/minimal-template.yaml."""
    path = _FIXTURES_DIR / "minimal-template.yaml"
    assert path.exists(), f"E2E fixture missing: {path}"
    return path


@pytest.fixture
def minimal_e2e_input() -> dict:
    """Return parsed dict from tests/fixtures/minimal-input.json."""
    path = _FIXTURES_DIR / "minimal-input.json"
    assert path.exists(), f"E2E fixture missing: {path}"
    return json.loads(path.read_text())


@pytest.fixture
def mock_openclaw_executor():
    """Factory fixture: creates a mock executor with configurable phase outputs.

    Usage::

        def test_something(mock_openclaw_executor):
            executor = mock_openclaw_executor(outputs={"spec": "My spec output"})
    """
    from orchestration_engine.schemas import TaskResult, TaskState, TaskType

    def _factory(outputs: dict = None, *, failure_phases: list = None):
        """Create a mock executor.

        Args:
            outputs: Map of phase_id → output text (used in result.result["message"])
            failure_phases: List of phase IDs that should return FAILED state
        """
        outputs = outputs or {}
        failure_phases = failure_phases or []

        def _execute(task, worker_id, model_tier=None, thinking_level=None):
            phase_id = getattr(task, "id", None) or getattr(task, "phase_id", "unknown")
            output_text = outputs.get(phase_id, f"Mock output for {phase_id}")
            state = TaskState.FAILED if phase_id in failure_phases else TaskState.SUCCESS

            result = MagicMock(spec=TaskResult)
            result.state = state
            result.task_id = f"mock-{phase_id}-001"
            result.task_type = TaskType.CONTENT
            result.confidence = 0.0 if state == TaskState.FAILED else 0.9
            result.errors = [{"code": "mock_failure", "message": "simulated"}] if state == TaskState.FAILED else []
            result.model_used = model_tier or "mock"
            result.tokens_consumed = 500
            result.cost_usd = 0.05
            result.execution_time_seconds = 0.01
            result.result = {
                "message": output_text,
                "model_used": model_tier or "mock",
                "worker_id": worker_id,
            }
            result.model_dump.return_value = {
                "task_id": f"mock-{phase_id}-001",
                "task_type": "content",
                "state": state.value,
                "confidence": result.confidence,
                "result": result.result,
                "errors": result.errors,
                "model_used": result.model_used,
                "tokens_consumed": 500,
                "cost_usd": 0.05,
                "execution_time_seconds": 0.01,
            }
            return result

        executor = MagicMock()
        executor.execute.side_effect = _execute
        executor.can_handle.return_value = True
        executor.estimate_cost.return_value = 0.01
        executor.request_shutdown = MagicMock()
        executor.cancel_active_session = MagicMock()
        executor._active_session_key = None
        return executor

    return _factory
