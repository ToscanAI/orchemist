"""Canonical timestamp / row normalisation helpers.

Single source of truth for the UTC-tagging logic that was previously
closure-scoped inside :func:`orchestration_engine.web.api.create_api_app`
and inline-duplicated in :mod:`orchestration_engine.db` (admin audit
``created_at`` column) and at the run-event payload site in ``web/api.py``.

Why a separate module:
  * ``db.py`` and ``web/api.py`` both need the helper, and ``db.py`` cannot
    import from ``web/api.py`` without inverting the layering.
  * Closure scope hid the helper from tests and other call sites.
  * Refactoring it requires editing one place, not three.

Z-suffix rationale:
  SQLite's ``CURRENT_TIMESTAMP`` writes naive UTC strings
  ("YYYY-MM-DDTHH:MM:SS"). JavaScript's ``new Date("2026-...")``
  interprets timezone-less strings as *local* time, so a CEST client
  (UTC+2) sees every "X min ago" off by +2h. We normalise on the way out:
  any string that looks like a naive ISO timestamp gets a trailing ``Z``
  appended so the client knows it is UTC. Already-tagged timestamps
  (``...Z``, ``...+00:00``, ``...-05:00``) pass through unchanged.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict

__all__ = ["normalize_ts", "normalize_row", "now_utc"]


def now_utc() -> datetime:
    """Current time as a timezone-aware UTC datetime.

    Canonical replacement for naive ``datetime.now()`` and deprecated
    ``datetime.utcnow()``. The aware ``+00:00`` form round-trips through
    ``datetime.fromisoformat`` and SQLite ``julianday()`` (verified), and
    ``normalize_ts`` passes an already-``+00:00`` isoformat through unchanged,
    so the public ISO-string output contract is preserved.
    """
    return datetime.now(timezone.utc)


_NAIVE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")


def normalize_ts(value: Any) -> Any:
    """Best-effort UTC normalisation of a single timestamp value.

    Behaviour:
      * ``None`` -> ``None`` (pass-through; callers that need a string
        coercion handle it themselves).
      * Object with ``isoformat`` (e.g. ``datetime``) -> call ``isoformat()``
        first, then apply the string rules below.
      * Naive ISO string ("YYYY-MM-DDTHH:MM:SS" with optional fractional
        seconds) -> return with a trailing ``Z`` appended.
      * Already-tagged string ("...Z", "...+00:00", etc.) -> pass through.
      * Anything else (int, dict, list, etc.) -> pass through unchanged.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    if isinstance(value, str) and _NAIVE_ISO_RE.match(value):
        return value + "Z"
    return value


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Defensive post-processor for one DB row before JSON serialisation.

    Two normalisations:

    1. Z-suffix any naive ISO timestamp value (JS interprets TZ-less strings
       as local time, off by the client's UTC offset).
    2. Columns declared as JSON arrays in the TS interface that ended up
       as a non-list (parse failure or hand-edit) are coerced back to
       ``[]`` so the frontend's ``.length`` / ``.slice()`` / ``.map()``
       calls don't throw on a corrupt row.

    The keys / list_keys lists below are inclusive of every shape returned
    by any harness endpoint at the time of writing. New keys can be added
    when new endpoints surface new timestamp / JSON-array columns.
    """
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for key in ("created_at", "updated_at", "completed_at", "started_at",
                "last_run_at", "last_updated"):
        if key in out:
            out[key] = normalize_ts(out[key])
    # ``commits`` is included for forward-compat: gate dicts (a different
    # response shape) have that key, and if any future endpoint passes
    # those rows through ``normalize_row`` we want the array-typed
    # contract preserved. Currently no harness endpoint actually returns
    # rows with ``commits``.
    for list_key in ("affected_files", "issues_found", "commits"):
        if list_key in out and not isinstance(out[list_key], list):
            out[list_key] = []
    return out
