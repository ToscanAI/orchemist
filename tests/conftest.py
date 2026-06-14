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
import sys
from pathlib import Path

import pytest

# Repository root resolved from this conftest location.
# Promoted to a public name (#876 D-4) so wiring-guard tests can import
# it from a single source.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Ensure THIS repo's `src/` directory wins over any installed editable
# `.pth` files that point at other worktrees. Without this, when multiple
# worktrees of orchemist coexist, `import orchestration_engine` may resolve
# to a sibling worktree's source instead of the one being tested. Inserting
# our src at index 0 forces deterministic local-source resolution.
_LOCAL_SRC = str(REPO_ROOT / "src")
if _LOCAL_SRC not in sys.path:
    sys.path.insert(0, _LOCAL_SRC)
elif sys.path[0] != _LOCAL_SRC:
    sys.path.remove(_LOCAL_SRC)
    sys.path.insert(0, _LOCAL_SRC)


def read_src(rel_path: str) -> str:
    """Return the text of a file under ``src/orchestration_engine/``.

    Helper for wiring-guard tests that grep source files to verify a given
    symbol / pattern is still present after a refactor.

    Example::

        from tests.conftest import read_src
        api_src = read_src("web/api.py")
        assert "_load_yaml_via_tempfile" in api_src

    Promoted in #876 (D-4) from 12 duplicated multi-line scaffolds of
    ``Path(__file__).resolve().parent.parent / "src" / ... .read_text()``.

    Package-aware (EPIC #942 god-module decomposition): when ``rel_path``
    names a module file (e.g. ``"cli.py"``) that has been converted into a
    package of the same stem (``cli/``), the file no longer exists. In that
    case this returns the concatenation of every ``*.py`` source file in the
    package tree, so existing wiring-guard greps keep matching the exact same
    source content regardless of which sub-module it now lives in. The grep
    semantics are unchanged — only the on-disk layout moved.
    """
    base = REPO_ROOT / "src" / "orchestration_engine"
    target = base / rel_path
    if target.exists():
        return target.read_text(encoding="utf-8")

    # File missing — it may have become a package (``foo.py`` -> ``foo/``).
    if rel_path.endswith(".py"):
        pkg_dir = base / rel_path[: -len(".py")]
        if pkg_dir.is_dir():
            parts = [p.read_text(encoding="utf-8") for p in sorted(pkg_dir.rglob("*.py"))]
            return "\n".join(parts)

    # Preserve the original FileNotFoundError for genuinely-missing paths.
    return target.read_text(encoding="utf-8")


@pytest.fixture
def examples_on_path(monkeypatch):
    """Add examples/ to ORCH_TEMPLATES_PATH so fixture templates are resolvable.

    Use this in test methods that invoke CLI or webhook with 'coding-pipeline-fixture'
    or any other template that lives in examples/ rather than templates/.

    Issue #632: tests must not depend on production templates in templates/ by
    filesystem path. Stable fixtures live in examples/.
    """
    existing = os.environ.get("ORCH_TEMPLATES_PATH", "")
    examples = str(REPO_ROOT / "examples")
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


@pytest.fixture(scope="session", autouse=True)
def _isolate_engine_db(tmp_path_factory):
    """#981: route the engine DB to a per-session tmp file so NO test (and no
    daemon subprocess any test spawns) writes the operator's real
    ~/.orchestration-engine/engine.db. Sets ORCH_DB_PATH (file path), which
    default_db_path() now honours; subprocesses inherit it via the parent env.
    Deliberately does NOT touch HOME, so test_ac10's --collect-only subprocess
    still resolves user-site pytest on a dev box.

    NOTE (#981 round-2): ORCH_DB_PATH is checked BEFORE Path.home() in
    default_db_path(), so this session env takes PRECEDENCE over any per-test
    HOME redirect. Pre-existing tests that assert a HOME-derived db path, or
    read back a HOME-derived Database() after a bare `orch run`, MUST delenv
    ORCH_DB_PATH function-scoped (see test_engine_consolidation's
    _unset_orch_db_path and the #980 _isolate_home fixtures). This fixture does
    NOT silently waive that — it is the cause of it.

    Why os.environ directly, not monkeypatch: pytest's monkeypatch fixture is
    function-scoped and cannot be requested by a session-scoped fixture
    (ScopeMismatch), so we mutate the real process environment and
    capture/restore the prior value on teardown.
    """
    db_path = tmp_path_factory.mktemp("engine-db") / "engine.db"
    prev = os.environ.get("ORCH_DB_PATH")
    os.environ["ORCH_DB_PATH"] = str(db_path)
    yield
    if prev is None:
        os.environ.pop("ORCH_DB_PATH", None)
    else:
        os.environ["ORCH_DB_PATH"] = prev


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
