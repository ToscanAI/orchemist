"""Cost-API query domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951d) WITHOUT
behavioural change. Holds the aggregated cost-reporting readers over the
``cost_tracking`` table (``get_cost_summary``, ``count_cost_summary``,
``get_run_costs``, Issue #5.2.3). Method bodies are byte-identical to the
original; only the import depth of intra-package references is adjusted. These
methods reference connection/transaction helpers (``self._locked`` /
``self.get_connection`` / ``self._row_to_dict``) resolved at runtime via the
MRO from :class:`~orchestration_engine.db._core.CoreMixin`.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

from typing import Any, Dict, List, Optional


class CostMixin:
    """Aggregated cost-reporting readers over the ``cost_tracking`` table.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``_locked``, ``get_connection``, ``_row_to_dict``)
    are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # ------------------------------------------------------------------
    # Cost API query methods (Issue #5.2.3)
    # ------------------------------------------------------------------

    def get_cost_summary(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        group_by: str = "day",
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return aggregated cost data grouped by day, template, or model.

        Args:
            start_date: Optional ISO date string ``YYYY-MM-DD`` (inclusive lower bound).
            end_date:   Optional ISO date string ``YYYY-MM-DD`` (inclusive upper bound).
            group_by:   One of ``"day"``, ``"template"``, or ``"model"``.
            limit:      Maximum number of rows to return.
            offset:     Number of rows to skip (pagination).

        Returns:
            List of dicts with aggregated cost statistics.  Each dict contains
            ``total_cost``, ``total_input_tokens``, ``total_output_tokens``,
            ``phase_count``, and a group key (``day``, ``template_id``, or
            ``model`` depending on ``group_by``).
        """
        params: List[Any] = []
        where_clauses: List[str] = []

        if start_date is not None:
            where_clauses.append("DATE(ct.created_at) >= ?")
            params.append(start_date)
        if end_date is not None:
            where_clauses.append("DATE(ct.created_at) <= ?")
            params.append(end_date)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        if group_by == "day":
            select_col = "DATE(ct.created_at) AS day"
            group_col = "DATE(ct.created_at)"
            order_sql = "ORDER BY day DESC"
            from_join = "FROM cost_tracking ct"
        elif group_by == "template":
            select_col = "pr.template_id"
            group_col = "pr.template_id"
            order_sql = "ORDER BY total_cost DESC"
            from_join = "FROM cost_tracking ct " "JOIN pipeline_runs pr ON ct.run_id = pr.run_id"
        else:  # group_by == "model"
            select_col = "ct.model"
            group_col = "ct.model"
            order_sql = "ORDER BY total_cost DESC"
            from_join = "FROM cost_tracking ct"

        sql = f"""
            SELECT
                {select_col},
                SUM(ct.cost_usd)      AS total_cost,
                SUM(ct.input_tokens)  AS total_input_tokens,
                SUM(ct.output_tokens) AS total_output_tokens,
                COUNT(*)              AS phase_count
            {from_join}
            {where_sql}
            GROUP BY {group_col}
            {order_sql}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count_cost_summary(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        group_by: str = "day",
    ) -> int:
        """Return the total number of groups for a cost summary query.

        Uses a subquery so the pagination metadata (``total``) can be
        computed without fetching all rows.

        Args:
            start_date: Optional ISO date string ``YYYY-MM-DD``.
            end_date:   Optional ISO date string ``YYYY-MM-DD``.
            group_by:   One of ``"day"``, ``"template"``, or ``"model"``.

        Returns:
            Integer count of distinct group values.
        """
        params: List[Any] = []
        where_clauses: List[str] = []

        if start_date is not None:
            where_clauses.append("DATE(ct.created_at) >= ?")
            params.append(start_date)
        if end_date is not None:
            where_clauses.append("DATE(ct.created_at) <= ?")
            params.append(end_date)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        if group_by == "day":
            group_col = "DATE(ct.created_at)"
            from_join = "FROM cost_tracking ct"
        elif group_by == "template":
            group_col = "pr.template_id"
            from_join = "FROM cost_tracking ct " "JOIN pipeline_runs pr ON ct.run_id = pr.run_id"
        else:  # group_by == "model"
            group_col = "ct.model"
            from_join = "FROM cost_tracking ct"

        sql = f"""
            SELECT COUNT(*) FROM (
                SELECT {group_col}
                {from_join}
                {where_sql}
                GROUP BY {group_col}
            )
        """

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(sql, params)
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def get_run_costs(self, run_id: str) -> List[Dict[str, Any]]:
        """Return all per-phase cost records for a specific pipeline run.

        Args:
            run_id: The pipeline run identifier.

        Returns:
            List of dicts from the ``cost_tracking`` table, ordered by
            ``created_at ASC``.  Empty list when no records exist for the run.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT *
                FROM cost_tracking
                WHERE run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]
