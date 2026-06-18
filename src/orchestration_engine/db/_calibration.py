"""Reviewer-calibration domain mixin for
:class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951e) WITHOUT
behavioural change. Holds the reviewer-calibration snapshot helpers
(``insert_calibration_snapshot``, ``get_calibration_for_model``,
``list_calibration_snapshots``, Issue #4.1.5). Method bodies are byte-identical
to the original; only the import depth of intra-package references is adjusted.
These methods reference connection/transaction helpers (``self.transaction`` /
``self._locked`` / ``self.get_connection`` / ``self._row_to_dict``) resolved at
runtime via the MRO from
:class:`~orchestration_engine.db._core.CoreMixin`.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

from typing import Any, Dict, List, Optional


class CalibrationMixin:
    """Reviewer-calibration snapshot readers/mutators.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # ------------------------------------------------------------------
    # Reviewer Calibration Operations (Issue #4.1.5)
    # ------------------------------------------------------------------

    def insert_calibration_snapshot(self, data: Dict[str, Any]) -> int:
        """Insert a reviewer calibration snapshot and return the rowid.

        Args:
            data: Dict with keys matching the ``reviewer_calibration`` table
                  columns (as produced by
                  :meth:`~reviewer_calibration.CalibrationMetrics.to_dict`):

                  - ``reviewer_model`` (str): Model tier/name.
                  - ``total_reviews`` (int): Total outcomes observed.
                  - ``approve_count`` (int): Number of APPROVE verdicts.
                  - ``request_changes_count`` (int): Number of RC verdicts.
                  - ``approve_held_up_count`` (int): APPROVEs with no fix.
                  - ``request_changes_valid_count`` (int): Verified RCs.
                  - ``approve_accuracy`` (float | None): APPROVE accuracy.
                  - ``request_changes_accuracy`` (float | None): RC accuracy.
                  - ``overall_accuracy`` (float | None): Combined accuracy.
                  - ``computed_at`` (str, optional): ISO-8601 timestamp.
                  - ``aggregation_window`` (str, optional): Time window label.

        Returns:
            The ``rowid`` of the newly inserted row (integer).
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reviewer_calibration
                    (reviewer_model, total_reviews, approve_count,
                     request_changes_count, approve_held_up_count,
                     request_changes_valid_count, approve_accuracy,
                     request_changes_accuracy, overall_accuracy,
                     computed_at, aggregation_window)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["reviewer_model"],
                    int(data.get("total_reviews", 0)),
                    int(data.get("approve_count", 0)),
                    int(data.get("request_changes_count", 0)),
                    int(data.get("approve_held_up_count", 0)),
                    int(data.get("request_changes_valid_count", 0)),
                    data.get("approve_accuracy"),
                    data.get("request_changes_accuracy"),
                    data.get("overall_accuracy"),
                    data.get("computed_at"),
                    data.get("aggregation_window"),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_calibration_for_model(
        self,
        reviewer_model: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent calibration snapshot for a given model.

        Args:
            reviewer_model: The model name/tier to look up (e.g. ``"opus"``).

        Returns:
            A calibration snapshot dict (most recent by ``computed_at``), or
            ``None`` when no snapshots exist for the model.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM reviewer_calibration
                WHERE reviewer_model = ?
                ORDER BY computed_at DESC
                LIMIT 1
                """,
                (reviewer_model,),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def list_calibration_snapshots(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return a paginated global listing of all calibration snapshots.

        Rows are ordered by ``computed_at DESC`` (newest first).  Use
        ``limit`` and ``offset`` for cursor-based pagination.

        Args:
            limit:  Maximum number of rows to return (default ``50``).
            offset: Number of rows to skip for pagination (default ``0``).

        Returns:
            List of calibration snapshot dicts ordered by ``computed_at DESC``.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM reviewer_calibration
                ORDER BY computed_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]
