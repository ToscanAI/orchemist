"""Diagnosis-result domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951e) WITHOUT
behavioural change. Holds the ``diagnosis_results`` CRUD/readers
(``insert_diagnosis``, ``get_diagnosis_by_run_id``, ``list_diagnoses``,
Issue #3.1.1). Method bodies are byte-identical to the original; only the import
depth of intra-package references is adjusted. These methods reference
connection/transaction helpers (``self.transaction`` / ``self._locked`` /
``self.get_connection`` / ``self._row_to_dict``) resolved at runtime via the MRO
from :class:`~orchestration_engine.db._core.CoreMixin`.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

from typing import Any, Dict, List, Optional


class DiagnosisMixin:
    """``diagnosis_results`` CRUD and readers.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # ------------------------------------------------------------------
    # Diagnosis Operations (Issue #3.1.1)
    # ------------------------------------------------------------------

    def insert_diagnosis(self, diagnosis_data: Dict[str, Any]) -> int:
        """Insert a DiagnosisResult record.

        Args:
            diagnosis_data: Dict with keys: run_id, failure_class, remediation,
                confidence, explanation, model_used, tokens_consumed.
                ``failure_class`` and ``remediation`` should be the .value of
                their respective enums (strings).

        Returns:
            The auto-incremented ``id`` of the inserted row.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO diagnosis_results
                    (run_id, failure_class, remediation, confidence,
                     explanation, model_used, tokens_consumed)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    diagnosis_data["run_id"],
                    diagnosis_data["failure_class"],
                    diagnosis_data["remediation"],
                    diagnosis_data["confidence"],
                    diagnosis_data.get("explanation"),
                    diagnosis_data.get("model_used"),
                    diagnosis_data.get("tokens_consumed", 0),
                ),
            )
            return cursor.lastrowid

    def get_diagnosis_by_run_id(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recent diagnosis for a run, or None.

        If multiple diagnoses exist for a run (e.g. re-diagnoses after retry),
        the most recently created one is returned.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM diagnosis_results
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT 1
            """,
                (run_id,),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def list_diagnoses(
        self,
        failure_class: Optional[str] = None,
        remediation: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List diagnosis results with optional filtering and pagination.

        Args:
            failure_class: Optional string value of FailureClass enum to filter by.
            remediation:   Optional string value of Remediation enum to filter by.
            limit:         Max rows to return (default 100).
            offset:        Rows to skip for pagination (default 0).

        Returns:
            List of diagnosis dicts ordered by ``id DESC`` (newest first).
        """
        query = "SELECT * FROM diagnosis_results WHERE 1=1"
        params: list = []

        if failure_class:
            query += " AND failure_class = ?"
            params.append(failure_class)

        if remediation:
            query += " AND remediation = ?"
            params.append(remediation)

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]
