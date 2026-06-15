"""Module-level constants, helpers, and sqlite3 type-adapter registration.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951a) WITHOUT
behavioural change. Importing this module performs the one-time sqlite3
datetime adapter/converter registration as a load-time side effect, exactly
as the original module did — the package facade (:mod:`db.__init__`) imports
this module so the registration still runs exactly once at package import.
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# Explicit datetime adapters — required for Python 3.12+ which deprecated
# the built-in sqlite3 datetime adapter/converter.
sqlite3.register_adapter(datetime, lambda val: val.isoformat())
sqlite3.register_converter("timestamp", lambda val: datetime.fromisoformat(val.decode()))


# ---------------------------------------------------------------------------
# Shared terminal-state set — single source of truth used by db, api, and
# any other module that needs to distinguish "run is done" from "run is live".
# Add new terminal statuses here; they propagate automatically everywhere.
# ---------------------------------------------------------------------------
TERMINAL_STATUSES: frozenset = frozenset(
    {
        "success",
        "failed",
        "cancelled",
        "crashed",
        "scoring_failed",
        "pending_review",
        "rejected",
        "escalated",  # Issue #396: retry was escalated — original run is terminal
    }
)


# ---------------------------------------------------------------------------
# Issue #932 (item 1) — staleness threshold for queue-health reporting.
# A task that has been in 'running' state strictly longer than this many
# minutes is considered stale. Single source of truth: matches the
# QueueStats docstring ("True if tasks stuck > 30min", schemas.py). queue.py
# consumes this via has_stale_running_tasks() rather than redefining it.
# ---------------------------------------------------------------------------
STALE_TASK_THRESHOLD_MINUTES = 30


# ---------------------------------------------------------------------------
# Issue #864 — canonical default DB path resolver
# ---------------------------------------------------------------------------
# Previously this logic was duplicated 5 ways across cli.py, web/api.py,
# mcp/tools.py, daemon.py, and inline inside Database.__init__.  The mcp
# variant was the only one creating parent directories (``parents=True``),
# so the canonical form preserves that behaviour — operators who haven't
# created ``~/.orchestration-engine`` see the path created on first access
# rather than a ``FileNotFoundError``.
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    """Return the canonical persistent on-disk DB path used by async runs.

    Resolves to ``$HOME/.orchestration-engine/engine.db`` and ensures the
    parent directory exists (``mkdir(parents=True, exist_ok=True)``).  This
    is the canonical location previously duplicated 5 ways across
    :mod:`cli`, :mod:`web.api`, :mod:`mcp.tools`, :mod:`daemon`, and inline
    inside :class:`Database`.

    Issue #981: an ``ORCH_DB_PATH`` env var (an absolute path to the
    ``engine.db`` *file*) takes precedence over the ``$HOME`` fallback when
    set, so operators/CI — and the pytest suite's session-scoped conftest
    fixture — can point the engine at a tmp DB without touching ``HOME``.
    Production behaviour is byte-identical when the var is unset/empty.
    Mirrors :func:`feature_flags._admin_json_path`'s ``ORCH_ADMIN_PATH``
    idiom; the override branch additionally ``mkdir``s the file's parent to
    preserve this function's documented parent-exists invariant (#864).

    Returns:
        ``Path`` pointing at the engine database file.  Callers that need
        a string path can wrap with ``str(default_db_path())``.
    """
    override = os.environ.get("ORCH_DB_PATH")
    if override:
        db_file = Path(override)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        return db_file
    default_dir = Path.home() / ".orchestration-engine"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir / "engine.db"


# ---------------------------------------------------------------------------
# Issue #866 — canonical JSON-list column parser
# ---------------------------------------------------------------------------
# The ``completed_phases`` column is stored as a JSON string in SQLite but
# may be returned by drivers as a native list (e.g. when wrapped by tests
# using TypedDict fixtures).  Both mcp/tools.py and the ``_run_to_dict``
# closure in web/api.py reinvented this parser; consolidating here keeps
# both call sites consistent if the column type ever changes.
# ---------------------------------------------------------------------------


def parse_json_list(val: Any) -> list:
    """Safely parse a JSON-list column that may be None, list, or JSON string.

    Used for the ``completed_phases`` column on ``pipeline_runs``.  Returns
    an empty list when *val* is ``None`` or cannot be decoded — callers
    should never receive a partial / malformed list from this helper.

    Args:
        val: Raw column value from a ``pipeline_runs`` row.  Tolerates
             ``None``, ``list`` (already decoded), or any other type that
             can be passed to :func:`json.loads`.

    Returns:
        A native Python list (possibly empty).  Never raises.
    """
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []
