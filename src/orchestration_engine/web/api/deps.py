"""Dependency providers for the REST API (Issue #942, sub-issue 952a).

Closure-free dependency/path providers extracted verbatim from ``web/api.py``
as part of the facade-preserving decomposition of the god-module. The
DB-handle/dependency providers that close over ``create_api_app``'s state
(``app``/``db``/``sequencer``) are NOT here — they stay in ``_app.py`` and are
deferred to the router-extraction sub-issues (952b-d). Only the closure-free
``_get_persistent_db_path`` moves here.
"""

from orchestration_engine.db import default_db_path


def _get_persistent_db_path() -> str:
    """Return the path to the persistent on-disk DB used by async runs.

    Thin string-returning wrapper around :func:`orchestration_engine.db.default_db_path`
    preserved for callsite signature compatibility (Issue #864 consolidation).
    """
    return str(default_db_path())
