"""Shared pytest fixtures and test configuration.

Provides session-scoped and autouse fixtures that handle global state cleanup
between test cases (e.g. the module-level circuit-breaker registry introduced
in issue #346).

Canonical fixture naming convention (issues #862 / #863 / #874):

  - ``db(tmp_path)`` — file-backed :class:`~orchestration_engine.db.Database`
    (exercises on-disk WAL semantics; preferred default).
  - ``in_memory_db()`` — ``:memory:`` Database for fast tests that do NOT
    need WAL semantics.
  - ``insert_pipeline_run(db)`` — callable that inserts a minimum-viable
    ``pipeline_runs`` row into the canonical ``db`` fixture. For tests using
    non-canonical DB fixtures, import the standalone version from
    ``tests._helpers``.
  - ``api_client(tmp_path, monkeypatch)`` — yields ``(TestClient, db_path)``
    bound to a fresh DB; ``ORCH_DB_PATH`` is set to the same path.
  - ``admin_json_isolated(tmp_path, monkeypatch)`` — yields a tmp admin.json
    Path; ``ORCH_ADMIN_PATH`` is set and ``feature_flags.reset_cache()`` is
    called both before and after the test.

Note: pytest fixture resolution falls back to conftest when a local fixture
is NOT defined with the same name. Any test that already declares its own
``db`` / ``in_memory_db`` / ``api_client`` fixture continues to use the
local override (no behaviour change). Migration is incremental.
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
# Issues #862 / #863 / #874 / #875 — canonical test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """File-backed :class:`Database` for a single test (#863).

    The DB lives at ``tmp_path / "engine.db"``. This is the preferred default
    because it exercises the on-disk WAL path that production daemons use.
    """
    from orchestration_engine.db import Database
    return Database(tmp_path / "engine.db")


@pytest.fixture
def in_memory_db():
    """Shared-cache ``:memory:`` :class:`Database` (#863).

    Use this only for fast tests that do NOT need WAL semantics (e.g. pure
    schema / model / FK tests). Prefer the file-backed :func:`db` fixture
    for anything that touches concurrent connections or sweep logic.
    """
    from orchestration_engine.db import Database
    return Database(":memory:")


@pytest.fixture
def insert_pipeline_run(db):
    """Callable that inserts a minimum-viable ``pipeline_runs`` row (#862).

    Bound to the canonical ``db`` fixture. Closes over ``db``, so each test
    that requests this fixture gets its own fresh file-backed DB.

    For tests that need to insert into a non-canonical DB (e.g. a
    class-scoped DB or one of multiple DBs), import the standalone
    :func:`tests._helpers.insert_pipeline_run` function instead.

    Returns:
        Callable ``_insert(*, run_id, status="pending", pid=None, **overrides) -> str``
        that returns the inserted ``run_id``.
    """
    from tests._helpers import insert_pipeline_run as _impl

    def _insert(*, run_id, status="pending", pid=None, **overrides):
        return _impl(db, run_id=run_id, status=status, pid=pid, **overrides)
    return _insert


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """Yield ``(TestClient, db_path)`` bound to a fresh DB (#874).

    Sets ``ORCH_DB_PATH`` so any module-level helper that reads the env var
    resolves to the same DB the TestClient sees. The TestClient is created
    via :func:`orchestration_engine.web.api.create_api_app`.
    """
    from fastapi.testclient import TestClient
    from orchestration_engine.web.api import create_api_app

    db_path = tmp_path / "engine.db"
    monkeypatch.setenv("ORCH_DB_PATH", str(db_path))
    client = TestClient(create_api_app(db_path=str(db_path)))
    return client, db_path


@pytest.fixture
def admin_json_isolated(tmp_path, monkeypatch):
    """Point ``feature_flags`` + admin API at a tmp ``admin.json`` (#874).

    Sets ``ORCH_ADMIN_PATH`` and resets the feature_flags TTL cache both
    before and after the test (yield). Returns the admin.json :class:`Path`.
    """
    from orchestration_engine import feature_flags as ff

    admin_path = tmp_path / "admin.json"
    monkeypatch.setenv("ORCH_ADMIN_PATH", str(admin_path))
    ff.reset_cache()
    yield admin_path
    ff.reset_cache()


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
