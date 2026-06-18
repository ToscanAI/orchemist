"""Issue-classification domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951d) WITHOUT
behavioural change. Holds the ``issue_pipeline_map`` CRUD/readers
(``insert_issue_classification``, ``get_issue_classification``,
``get_issue_classification_by_run_id``, ``get_issue_pipeline_map_by_run_id``,
``list_issue_classifications``, ``update_issue_classification_status``) and the
active-run deduplication reader (``get_active_issue_run``). Method bodies are
byte-identical to the original; only the import depth of intra-package
references is adjusted. These methods reference connection/transaction helpers
(``self.transaction`` / ``self._locked`` / ``self.get_connection`` /
``self._row_to_dict``) resolved at runtime via the MRO from
:class:`~orchestration_engine.db._core.CoreMixin`.

``get_active_issue_run`` remains a ``mock.patch`` target at
``orchestration_engine.db.Database.get_active_issue_run``; because db has a
trivial single-chain MRO and no ``super()``, patching the inherited method
resolves+restores cleanly through the MRO.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

from typing import Any, Dict, List, Optional

from ._consts import TERMINAL_STATUSES


class IssuesMixin:
    """``issue_pipeline_map`` CRUD plus active-run deduplication reader.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # ------------------------------------------------------------------
    # Issue Pipeline Map CRUD (Issue #5.1.1)
    # ------------------------------------------------------------------

    def insert_issue_classification(self, data: Dict[str, Any]) -> int:
        """Insert a new issue classification row and return the primary key.

        Args:
            data: Dict with keys matching the ``issue_pipeline_map`` table.
                  Required keys: ``issue_number``, ``repo``,
                  ``classification_type``, ``confidence``.
                  Optional: ``template_id``, ``run_id``, ``status``,
                  ``created_at``.

        Returns:
            The integer ``id`` (primary key) of the newly inserted row.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO issue_pipeline_map
                    (issue_number, repo, classification_type, confidence,
                     template_id, run_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(data["issue_number"]),
                    data["repo"],
                    data["classification_type"],
                    float(data["confidence"]),
                    data.get("template_id"),
                    data.get("run_id"),
                    data.get("status", "classified"),
                    data.get("created_at"),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_issue_classification(
        self,
        issue_number: int,
        repo: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent classification for an issue, or None.

        When the same issue has been classified multiple times (e.g. after a
        re-triage), the most recently inserted row is returned.

        Args:
            issue_number: GitHub issue number.
            repo:         Repository slug (e.g. ``"owner/repo"``).

        Returns:
            Dict with all ``issue_pipeline_map`` columns, or ``None`` when no
            matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM issue_pipeline_map
                WHERE issue_number = ? AND repo = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (issue_number, repo),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def get_issue_classification_by_run_id(
        self,
        run_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the issue_pipeline_map row associated with *run_id*, or ``None``.

        Queries ``issue_pipeline_map`` by ``run_id`` and returns the most
        recently inserted matching row.  Used by the daemon's result-posting
        hook to resolve the triggering issue context when only the run ID is
        known.

        Args:
            run_id: Pipeline run ID (UUID string).

        Returns:
            Dict with all ``issue_pipeline_map`` columns, or ``None`` when no
            matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM issue_pipeline_map
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def get_issue_pipeline_map_by_run_id(
        self,
        run_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the issue_pipeline_map row for *run_id* (Issue #5.1.4 public API).

        Thin wrapper around :meth:`get_issue_classification_by_run_id` providing
        the canonical name mandated by the spec.

        Args:
            run_id: Pipeline run ID (UUID string).

        Returns:
            Dict with all ``issue_pipeline_map`` columns, or ``None`` when no
            matching row exists.
        """
        return self.get_issue_classification_by_run_id(run_id)

    def list_issue_classifications(
        self,
        repo: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List issue classification rows, newest first.

        Args:
            repo:  Optional repository slug filter.  When ``None`` all repos
                   are included.
            limit: Maximum rows to return (default ``100``).

        Returns:
            List of classification dicts ordered by ``id DESC``.
        """
        query = "SELECT * FROM issue_pipeline_map WHERE 1=1"
        params: List[Any] = []

        if repo is not None:
            query += " AND repo = ?"
            params.append(repo)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def update_issue_classification_status(
        self,
        row_id: int,
        status: str,
    ) -> bool:
        """Update the ``status`` of an issue classification row.

        Args:
            row_id: Integer primary key of the row to update.
            status: New status string (e.g. ``"launched"``, ``"skipped"``).

        Returns:
            ``True`` if a row was found and updated, ``False`` otherwise.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE issue_pipeline_map SET status = ? WHERE id = ?",
                (status, row_id),
            )
            return cursor.rowcount > 0

    def get_active_issue_run(
        self,
        issue_number: int,
        repo: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the first active ``issue_pipeline_map`` row for *(issue_number, repo)*.

        An "active" row is one whose linked ``pipeline_run.status`` is **not**
        in :data:`TERMINAL_STATUSES`.  Rows with ``run_id IS NULL`` (classified
        but not yet launched) are excluded — they do not constitute an active
        run and should not block deduplication.

        This is used by the GitHub issues webhook handler to prevent launching
        a duplicate pipeline when one is already running for the same issue.

        Args:
            issue_number: GitHub issue number.
            repo:         Repository slug (e.g. ``"owner/repo"``).

        Returns:
            Dict with all ``issue_pipeline_map`` columns for the first matching
            row, or ``None`` when no active run exists.
        """
        terminal_list = list(TERMINAL_STATUSES)
        placeholders = ",".join("?" * len(terminal_list))
        sql = f"""
            SELECT ipm.*
            FROM issue_pipeline_map ipm
            INNER JOIN pipeline_runs pr ON ipm.run_id = pr.run_id
            WHERE ipm.issue_number = ?
              AND ipm.repo = ?
              AND pr.status NOT IN ({placeholders})
            LIMIT 1
        """
        params: List[Any] = [issue_number, repo] + terminal_list

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(sql, params)
            row = cursor.fetchone()

        return self._row_to_dict(row) if row else None
