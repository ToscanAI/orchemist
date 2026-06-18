"""Routing-decision domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951e) WITHOUT
behavioural change. Holds the ``routing_decisions`` CRUD/readers
(``insert_routing_decision``, ``get_routing_decisions``, ``get_routing_decision``,
Issue #331.3). Method bodies are byte-identical to the original; only the import
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


class RoutingMixin:
    """``routing_decisions`` CRUD and readers.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # ------------------------------------------------------------------
    # Routing Decision Operations (Issue #331.3)
    # ------------------------------------------------------------------

    def insert_routing_decision(self, decision_data: dict) -> int:
        """Insert a routing decision record and return the auto-incremented id.

        Args:
            decision_data: Dict with keys:
                - run_id (str): The pipeline run identifier.
                - confidence_score (float): Composite confidence score in [0, 1].
                - tier_name (str): Matched routing tier name (e.g. "auto_merge").
                - action (str): Dispatched action (e.g. "auto_merge", "human_review").
                - justification (str, optional): Human-readable explanation.
                - signals_json (str): JSON-serialised signal dict.

        Returns:
            The ``id`` of the newly inserted row.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO routing_decisions
                    (run_id, confidence_score, tier_name, action, justification, signals_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_data["run_id"],
                    float(decision_data["confidence_score"]),
                    decision_data["tier_name"],
                    decision_data["action"],
                    decision_data.get("justification"),
                    decision_data.get("signals_json", "{}"),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_routing_decisions(self, run_id: str) -> List[Dict]:
        """Return all routing decision rows for a given pipeline run.

        Args:
            run_id: The pipeline run identifier to look up.

        Returns:
            List of routing decision dicts ordered by ``id ASC``.
            Returns an empty list when no decisions exist for the run.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM routing_decisions WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_routing_decision(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recent routing decision row for a given pipeline run.

        Convenience method that returns a single dict (the latest decision)
        rather than the full list returned by :meth:`get_routing_decisions`.

        Args:
            run_id: The pipeline run identifier to look up.

        Returns:
            The most recent routing decision dict (``signals_json`` parsed to a
            Python dict), or ``None`` when no decision has been recorded for
            the run.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM routing_decisions WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                (run_id,),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None
