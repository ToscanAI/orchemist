"""#981: engine-DB isolation guards.

Three tests live here, each managing its OWN ``ORCH_DB_PATH`` state via the
function-scoped ``monkeypatch`` fixture (there is deliberately NO file-level
autouse delenv in this file — the sentinel below RELIES on the session
``ORCH_DB_PATH`` being set by the root-conftest ``_isolate_engine_db``
fixture):

  * ``test_default_db_path_isolated_during_session`` — the assert-by-construction
    real-DB sentinel (req 4): during the test session ``default_db_path()``
    resolves to the session-injected tmp file, never the operator's real
    ``~/.orchestration-engine/engine.db``. SET-relying.
  * ``test_orch_db_path_override`` — the SET-behaviour unit test for the
    ``default_db_path()`` seam (the one place that asserts the override path is
    honoured, including the mkdir-parent side effect).
  * ``test_orch_db_path_unset_falls_back`` — the UNSET fallback unit test; it
    ``delenv``s the session override and patches ``Path.home`` so the byte-
    identical ``$HOME``-derived fallback is exercised without touching real
    ``$HOME``.
"""

import os
from pathlib import Path


def test_default_db_path_isolated_during_session():
    """Req 4 sentinel: default_db_path() is isolated for the whole session.

    Because the root-conftest session fixture sets ORCH_DB_PATH to a per-session
    tmp file, default_db_path() resolves there (NOT the real operator DB) by
    construction for every test in the suite — including the daemon-spawning
    leakers. This is the deterministic, xdist-safe proof that no test write can
    reach ~/.orchestration-engine/engine.db.
    """
    from orchestration_engine.db import default_db_path

    resolved = default_db_path()
    real = Path.home() / ".orchestration-engine" / "engine.db"
    assert (
        resolved != real
    ), f"default_db_path() resolved to the REAL operator DB during tests: {resolved}"
    # And it points at the session-injected ORCH_DB_PATH file:
    override = os.environ.get("ORCH_DB_PATH")
    assert override, "session fixture must have set ORCH_DB_PATH"
    assert resolved == Path(override)


def test_orch_db_path_override(tmp_path, monkeypatch):
    """SET behaviour: default_db_path() honours ORCH_DB_PATH and mkdirs its parent."""
    from orchestration_engine.db import default_db_path

    target = tmp_path / "sub" / "x.db"
    monkeypatch.setenv("ORCH_DB_PATH", str(target))
    assert default_db_path() == target
    assert target.parent.is_dir()  # mkdir-parent side effect on the override branch


def test_orch_db_path_unset_falls_back(tmp_path, monkeypatch):
    """UNSET behaviour: byte-identical $HOME-derived fallback.

    The session fixture sets ORCH_DB_PATH suite-wide, so this test must delenv it
    (function-scoped → overrides the session env, auto-restores on teardown) to
    exercise the fallback. It also patches Path.home so the assertion does not
    depend on (or create under) the real $HOME.
    """
    from orchestration_engine import db as db_module

    monkeypatch.delenv("ORCH_DB_PATH", raising=False)
    monkeypatch.setattr(db_module.Path, "home", lambda: tmp_path)
    assert db_module.default_db_path() == tmp_path / ".orchestration-engine" / "engine.db"
