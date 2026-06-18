"""Trigger / webhook-invocation domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951d) WITHOUT
behavioural change. Holds the trigger-config CRUD (``create_trigger``,
``get_trigger``, ``list_triggers``, ``update_trigger``, ``delete_trigger``)
and the webhook-invocation rate-limit helpers (``record_webhook_invocation``,
``count_webhook_invocations_since``). Method bodies are byte-identical to the
original; only the import depth of intra-package references is adjusted. These
methods reference connection/transaction helpers (``self.transaction`` /
``self._locked`` / ``self.get_connection`` / ``self._row_to_dict``) resolved at
runtime via the MRO from
:class:`~orchestration_engine.db._core.CoreMixin`.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..timestamps import now_utc


class TriggersMixin:
    """Trigger-config CRUD and webhook-invocation rate-limit readers.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # --- Trigger CRUD Operations (Issue #329.1) ---

    def create_trigger(self, trigger_data: Dict[str, Any]) -> str:
        """Insert a new trigger configuration row.

        Args:
            trigger_data: A plain dict as returned by
                ``TriggerConfig.to_dict()``.  Must contain ``'id'`` and
                ``'template_id'``.  ``input_map`` and ``filters`` must be
                Python dict/list (not pre-serialised JSON strings) — this
                method performs the JSON serialisation.

        Returns:
            The trigger ``id``.

        Raises:
            sqlite3.IntegrityError: If a trigger with the same ``id`` already
                exists.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO triggers
                    (id, template_id, mode, secret, rate_limit, input_map, filters, created_at, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    trigger_data["id"],
                    trigger_data["template_id"],
                    trigger_data.get("mode", "async"),
                    trigger_data.get("secret"),
                    trigger_data.get("rate_limit", 0),
                    json.dumps(trigger_data.get("input_map") or {}),
                    json.dumps(trigger_data.get("filters") or []),
                    trigger_data.get("created_at") or now_utc().isoformat(),
                    int(trigger_data.get("enabled", True)),
                ),
            )
        return trigger_data["id"]

    def get_trigger(self, trigger_id: str) -> Optional[Dict[str, Any]]:
        """Return a trigger config row by id, or None if not found.

        Args:
            trigger_id: The trigger identifier to look up.

        Returns:
            A dict with all trigger fields (JSON columns parsed to Python
            objects), or ``None`` if no matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute("SELECT * FROM triggers WHERE id = ?", (trigger_id,))
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def list_triggers(
        self,
        template_id: Optional[str] = None,
        mode: Optional[str] = None,
        enabled: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List trigger configs with optional filtering and pagination.

        Args:
            template_id: Filter by template id.
            mode: Filter by execution mode (``'sync'``, ``'async'``,
                ``'fire_and_forget'``).
            enabled: When provided, filters to only enabled (``True``) or
                disabled (``False``) triggers.
            limit: Maximum rows to return (default 100).
            offset: Rows to skip for pagination (default 0).

        Returns:
            List of trigger dicts ordered by ``created_at DESC``.
        """
        query = "SELECT * FROM triggers WHERE 1=1"
        params: list = []

        if template_id:
            query += " AND template_id = ?"
            params.append(template_id)

        if mode:
            query += " AND mode = ?"
            params.append(mode)

        if enabled is not None:
            query += " AND enabled = ?"
            params.append(int(enabled))

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

        return [self._row_to_dict(row) for row in rows]

    def update_trigger(self, trigger_id: str, **kwargs) -> bool:
        """Update whitelisted fields on a trigger config row.

        ``updated_at`` is always refreshed when at least one valid field is
        supplied.  Unknown kwargs are silently ignored.

        Allowed kwargs: ``mode``, ``secret``, ``rate_limit``,
        ``input_map``, ``filters``.

        Args:
            trigger_id: The trigger identifier to update.
            **kwargs: Field name → new value pairs.

        Returns:
            ``True`` if a DB row was modified, ``False`` if the trigger was
            not found **or** no valid fields were supplied.

        Note:
            A return value of ``False`` does not distinguish "trigger not
            found" from "no valid kwargs".  Callers that need to distinguish
            these cases should call ``get_trigger`` first.
        """
        allowed = {"mode", "secret", "rate_limit", "input_map", "filters", "enabled"}
        updates = ["updated_at = ?"]
        values = [now_utc().isoformat()]

        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key in ("input_map", "filters"):
                updates.append(f"{key} = ?")
                values.append(json.dumps(value))
            elif key == "enabled":
                updates.append(f"{key} = ?")
                values.append(int(value))
            else:
                updates.append(f"{key} = ?")
                values.append(value)

        # Only updated_at — no valid fields were provided
        if len(updates) == 1:
            return False

        values.append(trigger_id)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE triggers SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            return cursor.rowcount > 0

    def delete_trigger(self, trigger_id: str) -> bool:
        """Delete a trigger config by id.

        Args:
            trigger_id: The trigger identifier to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no matching row
            was found.
        """
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
            return cursor.rowcount > 0

    def record_webhook_invocation(self, trigger_id: str) -> None:
        """Record a webhook invocation timestamp for rate-limit tracking.

        Args:
            trigger_id: The ID of the trigger that was invoked.
        """
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO webhook_invocations (trigger_id, invoked_at) VALUES (?, ?)",
                (trigger_id, now_utc().isoformat()),
            )

    def count_webhook_invocations_since(self, trigger_id: str, since_dt: datetime) -> int:
        """Count webhook invocations for a trigger since a given datetime.

        Used for per-trigger rate-limit enforcement.

        Args:
            trigger_id: The trigger identifier to count invocations for.
            since_dt: Datetime lower bound (inclusive).

        Returns:
            Number of invocation rows with ``invoked_at >= since_dt``.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT COUNT(*) FROM webhook_invocations "
                "WHERE trigger_id = ? AND invoked_at >= ?",
                (trigger_id, since_dt.isoformat()),
            )
            return cursor.fetchone()[0]
