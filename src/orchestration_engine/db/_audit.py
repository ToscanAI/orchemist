"""Admin-audit-log domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951e) WITHOUT
behavioural change. Holds the admin-audit-log append/read helpers
(``append_admin_audit``, ``list_admin_audit``). Method bodies are
byte-identical to the original; only the import depth of intra-package
references is adjusted (the in-method lazy ``import json``/``import os`` are kept
lazy). These methods reference connection/transaction helpers
(``self.transaction``) resolved at runtime via the MRO from
:class:`~orchestration_engine.db._core.CoreMixin`.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

from typing import Any, Dict, List, Optional

from ..timestamps import normalize_ts


class AuditMixin:
    """Admin-audit-log append/read operations.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    def append_admin_audit(
        self,
        action: str,
        target: str,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        source_pid: Optional[int] = None,
    ) -> int:
        """Append a row to ``admin_audit_log``. Returns the new row id.

        Args:
            action: Short verb describing what changed (e.g.
                ``"update_feature_flags"``, ``"reset_admin_state"``).
            target: Which surface was mutated (e.g.
                ``"feature_flags"``, ``"autonomy_level"``, ``"modes"``).
                Multiple targets per action are concatenated comma-separated.
            before: Pre-mutation value (dict or None when first write).
            after: Post-mutation value.
            source_pid: OS pid of the FastAPI worker process. Default
                ``os.getpid()`` if not supplied.
        """
        import json as _json  # noqa: PLC0415
        import os as _os  # noqa: PLC0415

        pid = source_pid if source_pid is not None else _os.getpid()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO admin_audit_log
                    (action, target, before_json, after_json, source_pid)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    action,
                    target,
                    _json.dumps(before) if before is not None else None,
                    _json.dumps(after) if after is not None else None,
                    pid,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_admin_audit(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """Return up to ``limit`` recent admin audit rows, newest first.

        Each row is a dict with the columns of ``admin_audit_log``;
        ``before_json``/``after_json`` are parsed back into dicts (or None).
        """
        import json as _json  # noqa: PLC0415

        with self.transaction() as conn:
            cur = conn.execute(
                """
                SELECT id, action, target, before_json, after_json,
                       source_pid, created_at
                  FROM admin_audit_log
                 ORDER BY created_at DESC, id DESC
                 LIMIT ? OFFSET ?
                """,
                (int(limit), int(offset)),
            )
            rows: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                before = _json.loads(r["before_json"]) if r["before_json"] else None
                after = _json.loads(r["after_json"]) if r["after_json"] else None
                # Normalise created_at — SQLite returns a datetime object
                # when PARSE_DECLTYPES is set (see get_connection), but the
                # API surface is JSON so we need a string. ``normalize_ts``
                # (from ``.timestamps``) handles datetime -> isoformat and
                # Z-suffixes naive UTC strings so JS clients don't
                # misinterpret them as local time. (#876)
                created_str = normalize_ts(r["created_at"])
                rows.append(
                    {
                        "id": r["id"],
                        "action": r["action"],
                        "target": r["target"],
                        "before": before,
                        "after": after,
                        "source_pid": r["source_pid"],
                        "created_at": created_str,
                    }
                )
            return rows
