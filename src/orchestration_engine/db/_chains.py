"""Sprint-chain + chain-query domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951d) WITHOUT
behavioural change. Holds the sprint_chain_state CRUD/readers
(``upsert_sprint_chain_state``, ``get_sprint_chain_state``,
``get_sprint_processed_issues``, ``get_sprint_chain_states``) and the
recursive chain-walk queries over ``pipeline_runs`` (``get_full_chain``,
``list_active_chain_roots``, Issue #508). Method bodies are byte-identical to
the original; only the import depth of intra-package references is adjusted.
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

from ._consts import TERMINAL_STATUSES


class ChainsMixin:
    """Sprint-chain state CRUD and recursive chain-walk query readers.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    def upsert_sprint_chain_state(
        self,
        repo: str,
        issue_number: int,
        status: str,
        run_id: Optional[str] = None,
        score: Optional[float] = None,
    ) -> None:
        """Insert or update a sprint_chain_state row for ``(repo, issue_number)``.

        Uses ``INSERT OR REPLACE`` for idempotent upsert; updates
        ``processed_at`` to the current timestamp on each call.

        Args:
            repo:         Repository slug.
            issue_number: GitHub issue number.
            status:       ``"processed"`` or ``"paused"``.
            run_id:       Pipeline run_id (optional).
            score:        Confidence score (optional).
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sprint_chain_state
                    (repo, issue_number, status, run_id, score, processed_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(repo, issue_number)
                DO UPDATE SET
                    status       = excluded.status,
                    run_id       = excluded.run_id,
                    score        = excluded.score,
                    processed_at = CURRENT_TIMESTAMP
                """,
                (repo, issue_number, status, run_id, score),
            )

    def get_sprint_chain_state(self, repo: str, issue_number: int) -> Optional[Dict[str, Any]]:
        """Return the sprint_chain_state row for ``(repo, issue_number)``, or ``None``.

        Args:
            repo:         Repository slug.
            issue_number: GitHub issue number.

        Returns:
            Row dict with keys ``id``, ``repo``, ``issue_number``, ``status``,
            ``run_id``, ``score``, ``processed_at``, or ``None`` if not found.
        """
        conn = self.get_connection()
        cursor = conn.execute(
            "SELECT * FROM sprint_chain_state WHERE repo = ? AND issue_number = ?",
            (repo, issue_number),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    def get_sprint_processed_issues(self, repo: str) -> List[int]:
        """Return issue numbers marked ``"processed"`` for the given repo.

        Args:
            repo: Repository slug.

        Returns:
            List of issue numbers ordered by ``processed_at`` ascending.
        """
        conn = self.get_connection()
        cursor = conn.execute(
            """
            SELECT issue_number FROM sprint_chain_state
            WHERE repo = ? AND status = 'processed'
            ORDER BY processed_at ASC
            """,
            (repo,),
        )
        return [row[0] for row in cursor.fetchall()]

    def get_sprint_chain_states(self, repo: str) -> List[Dict[str, Any]]:
        """Return all sprint_chain_state rows for the given repo.

        Args:
            repo: Repository slug.

        Returns:
            List of row dicts ordered by ``processed_at`` ascending.
        """
        conn = self.get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM sprint_chain_state
            WHERE repo = ?
            ORDER BY processed_at ASC
            """,
            (repo,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Chain query methods (Issue #508)
    # ------------------------------------------------------------------

    def get_full_chain(self, root_run_id: str) -> List[Dict[str, Any]]:
        """Return all runs in a chain starting from *root_run_id* (inclusive).

        Uses a recursive CTE to walk *down* the parent→child tree.  The root
        run is returned first (depth 0), then children ordered by created_at.

        Args:
            root_run_id: The run_id of the chain root.

        Returns:
            Ordered list of pipeline_run dicts (root first, then descendants).
        """
        query = """
            WITH RECURSIVE chain(run_id, depth) AS (
                SELECT run_id, 0 FROM pipeline_runs WHERE run_id = ?
                UNION ALL
                SELECT pr.run_id, chain.depth + 1
                FROM pipeline_runs pr
                JOIN chain ON pr.parent_run_id = chain.run_id
                WHERE chain.depth < 50
            )
            SELECT pr.*
            FROM pipeline_runs pr
            JOIN chain ON pr.run_id = chain.run_id
            ORDER BY chain.depth ASC, pr.created_at ASC
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, (root_run_id,))
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_active_chain_roots(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return all root runs that have at least one non-terminal descendant.

        A *root* is a run with no parent (parent_run_id IS NULL).  A chain is
        *active* when any run in the chain is not in TERMINAL_STATUSES.

        Args:
            limit: Optional maximum number of roots to return.

        Returns:
            List of root pipeline_run dicts, ordered by created_at DESC.
        """
        terminal_list = list(TERMINAL_STATUSES)
        placeholders = ",".join("?" * len(terminal_list))
        query = f"""
            WITH RECURSIVE chain(root_id, run_id) AS (
                SELECT run_id, run_id
                FROM pipeline_runs
                WHERE parent_run_id IS NULL
                UNION ALL
                SELECT chain.root_id, pr.run_id
                FROM pipeline_runs pr
                JOIN chain ON pr.parent_run_id = chain.run_id
            )
            SELECT DISTINCT pr.*
            FROM pipeline_runs pr
            WHERE pr.parent_run_id IS NULL
              AND EXISTS (
                  SELECT 1 FROM chain c
                  JOIN pipeline_runs pr2 ON c.run_id = pr2.run_id
                  WHERE c.root_id = pr.run_id
                    AND pr2.status NOT IN ({placeholders})
              )
            ORDER BY pr.created_at DESC
        """
        params: List[Any] = terminal_list
        if limit is not None:
            query += " LIMIT ?"
            params = terminal_list + [limit]
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]
