"""Failure-pattern domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951e) WITHOUT
behavioural change. Holds the ``failure_patterns`` upsert/readers
(``insert_or_update_failure_pattern``, ``get_failure_patterns``, Issue #3.1.3).
Method bodies are byte-identical to the original; only the import depth of
intra-package references is adjusted. These methods reference
connection/transaction helpers (``self.transaction`` / ``self._locked`` /
``self.get_connection`` / ``self._row_to_dict``) resolved at runtime via the MRO
from :class:`~orchestration_engine.db._core.CoreMixin`.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

from typing import Any, Dict, List, Optional


class FailurePatternMixin:
    """``failure_patterns`` upsert and readers.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # ------------------------------------------------------------------
    # Failure pattern CRUD (Issue #3.1.3)
    # ------------------------------------------------------------------

    def insert_or_update_failure_pattern(
        self,
        pattern_hash: str,
        template_id: str,
        failure_class: str,
        now_iso: str,
        systemic_threshold: int = 3,
        systemic_window_days: int = 7,
    ) -> Dict[str, Any]:
        """Upsert a failure pattern record and mark as systemic when threshold exceeded.

        Inserts a new row on the first occurrence of *pattern_hash* + *template_id*.
        On subsequent occurrences the ``occurrence_count`` and ``last_seen_at``
        columns are updated atomically.  The ``is_systemic`` flag is set to
        ``1`` when ``occurrence_count`` reaches *systemic_threshold* **and** the
        elapsed time between ``first_seen_at`` and *now_iso* does not exceed
        *systemic_window_days*.

        Args:
            pattern_hash:        SHA-256 hex digest of the normalised error message.
            template_id:         Template identifier the failure belongs to.
            failure_class:       String value of the :class:`FailureClass` enum.
            now_iso:             Current timestamp in ISO-8601 format.
            systemic_threshold:  Minimum occurrences to be considered systemic
                                 (default ``3``).
            systemic_window_days: Maximum age (in days) of the first occurrence
                                  for the pattern to still be considered systemic
                                  (default ``7``).

        Returns:
            The upserted row as a ``dict``, including the updated
            ``occurrence_count`` and ``is_systemic`` flag.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO failure_patterns
                    (pattern_hash, template_id, failure_class, occurrence_count,
                     is_systemic, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, 1, 0, ?, ?)
                ON CONFLICT(pattern_hash, template_id) DO UPDATE SET
                    occurrence_count = occurrence_count + 1,
                    last_seen_at = excluded.last_seen_at,
                    is_systemic = CASE
                        WHEN (occurrence_count + 1) >= ?
                             AND (julianday(excluded.last_seen_at)
                                  - julianday(first_seen_at)) <= ?
                        THEN 1
                        ELSE is_systemic
                    END
                """,
                (
                    pattern_hash,
                    template_id,
                    failure_class,
                    now_iso,
                    now_iso,
                    systemic_threshold,
                    systemic_window_days,
                ),
            )
            cursor = conn.execute(
                "SELECT * FROM failure_patterns WHERE pattern_hash = ? AND template_id = ?",
                (pattern_hash, template_id),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else {}

    def get_failure_patterns(
        self,
        template_id: Optional[str] = None,
        systemic_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List failure patterns with optional filtering and pagination.

        Args:
            template_id:   If set, return only patterns for this template.
            systemic_only: If ``True``, return only systemic patterns
                           (``is_systemic = 1``).
            limit:         Maximum rows to return (default ``100``).
            offset:        Rows to skip for pagination (default ``0``).

        Returns:
            List of failure pattern dicts ordered by ``last_seen_at DESC``.
        """
        clauses: List[str] = []
        params: List[Any] = []

        if template_id is not None:
            clauses.append("template_id = ?")
            params.append(template_id)
        if systemic_only:
            clauses.append("is_systemic = 1")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                f"SELECT * FROM failure_patterns {where} "
                f"ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
                params,
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]
