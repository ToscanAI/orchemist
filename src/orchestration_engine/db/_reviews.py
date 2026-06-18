"""Review-queue / regression / CI-green / review-outcome domain mixin for
:class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951e) WITHOUT
behavioural change. Holds the review-queue readers/mutators
(``list_pending_reviews``, ``count_pending_reviews``, ``approve_pipeline_run``,
``reject_pipeline_run``), the regression CRUD (``insert_regression``,
``get_regression``, ``update_regression``, ``list_regressions``), the
CI-green-SHA tracking helpers (``store_green_sha``, ``get_last_green_sha``) and
the review-outcome readers/mutators (``insert_review_outcome``,
``get_review_outcomes_for_run``, ``list_review_outcomes``). Method bodies are
byte-identical to the original; only the import depth of intra-package
references is adjusted. These methods reference connection/transaction helpers
(``self.transaction`` / ``self._locked`` / ``self.get_connection`` /
``self._row_to_dict``) resolved at runtime via the MRO from
:class:`~orchestration_engine.db._core.CoreMixin`.

``approve_pipeline_run`` / ``reject_pipeline_run`` remain ``mock.patch``-safe at
``orchestration_engine.db.Database.<name>``; db has a trivial single-chain MRO
and no ``super()``, so patching an inherited method resolves+restores cleanly.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..timestamps import now_utc


class ReviewsMixin:
    """Review-queue, regression, CI-green-SHA and review-outcome operations.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # ------------------------------------------------------------------
    # Review Queue Operations (Issue #331.4)
    # ------------------------------------------------------------------

    def list_pending_reviews(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return pipeline runs with status='pending_review', enriched with routing decision data.

        Performs a LEFT JOIN against ``routing_decisions`` to include the
        latest confidence score and tier for each pending run.

        Args:
            limit: Maximum number of rows to return (default 20).
            offset: Number of rows to skip for pagination (default 0).

        Returns:
            List of dicts, each containing all pipeline_runs columns plus
            ``confidence_score`` and ``tier_name`` from the most recent
            routing decision (or ``None`` when no decision exists).
        """
        query = """
            SELECT pr.*,
                   rd.confidence_score,
                   rd.tier_name,
                   rd.action,
                   rd.justification
            FROM pipeline_runs pr
            LEFT JOIN (
                SELECT run_id,
                       confidence_score,
                       tier_name,
                       action,
                       justification,
                       MAX(id) AS max_id
                FROM routing_decisions
                GROUP BY run_id
            ) rd ON pr.run_id = rd.run_id
            WHERE pr.status = 'pending_review'
            ORDER BY pr.created_at DESC
            LIMIT ? OFFSET ?
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, (limit, offset))
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count_pending_reviews(self) -> int:
        """Return the total count of pipeline runs with status='pending_review'.

        Returns:
            Integer count of pending review runs.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT COUNT(*) FROM pipeline_runs WHERE status = 'pending_review'"
            )
            row = cursor.fetchone()
        return row[0] if row else 0

    def approve_pipeline_run(
        self,
        run_id: str,
        reviewed_by: Optional[str] = None,
        note: Optional[str] = None,
    ) -> bool:
        """Approve a pending_review pipeline run, setting status to 'success'.

        Args:
            run_id: The pipeline run identifier to approve.
            reviewed_by: Optional identifier of the reviewer (user/system).
            note: Optional review note stored in review_reason.

        Returns:
            ``True`` if a row was updated, ``False`` if no matching
            pending_review run was found.
        """
        now = now_utc().isoformat()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE pipeline_runs
                SET status = 'success',
                    review_reason = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE run_id = ? AND status = 'pending_review'
                """,
                (note, now, reviewed_by, now, run_id),
            )
            return cursor.rowcount > 0

    def reject_pipeline_run(
        self,
        run_id: str,
        reason: str,
        reviewed_by: Optional[str] = None,
    ) -> bool:
        """Reject a pending_review pipeline run, setting status to 'rejected'.

        Args:
            run_id: The pipeline run identifier to reject.
            reason: Human-readable rejection reason stored in review_reason.
            reviewed_by: Optional identifier of the reviewer.

        Returns:
            ``True`` if a row was updated, ``False`` if no matching
            pending_review run was found.
        """
        now = now_utc().isoformat()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE pipeline_runs
                SET status = 'rejected',
                    review_reason = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE run_id = ? AND status = 'pending_review'
                """,
                (reason, now, reviewed_by, now, run_id),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Regression CRUD (Issue #3.3a.1)
    # ------------------------------------------------------------------

    def insert_regression(self, regression_data: Dict[str, Any]) -> str:
        """Insert a new regression record.

        Args:
            regression_data: Dict matching the Regression dataclass fields.
                ``affected_files`` may be a Python list or an already
                JSON-serialised string (use ``Regression.to_dict()`` for
                the canonical format).

        Returns:
            The ``id`` of the inserted row.
        """
        import json as _json  # noqa: PLC0415

        # Normalise affected_files: accept both list and pre-serialised string.
        af = regression_data.get("affected_files", [])
        if isinstance(af, str):
            af_serialised = af
        else:
            af_serialised = _json.dumps(af)

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO regressions
                    (id, commit_sha, ci_run_url, failure_type, affected_files,
                     diagnosis, fix_run_id, status, fix_attempt_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    regression_data["id"],
                    regression_data["commit_sha"],
                    regression_data["ci_run_url"],
                    regression_data["failure_type"],
                    af_serialised,
                    regression_data.get("diagnosis"),
                    regression_data.get("fix_run_id"),
                    regression_data.get("status", "detected"),
                    regression_data.get("fix_attempt_count", 0),
                    regression_data.get("created_at"),
                ),
            )
        return regression_data["id"]

    def get_regression(self, regression_id: str) -> Optional[Dict[str, Any]]:
        """Return a regression record by id, or None if not found.

        Args:
            regression_id: UUID of the regression to retrieve.

        Returns:
            Dict with all regression fields (``affected_files`` deserialised
            to a Python list), or ``None`` if no matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute("SELECT * FROM regressions WHERE id = ?", (regression_id,))
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def update_regression(self, regression_id: str, **kwargs: Any) -> bool:
        """Update fields on a regressions row.

        Only the following fields may be updated:
        ``status``, ``diagnosis``, ``fix_run_id``, ``fix_attempt_count``.
        Unrecognised kwargs are silently ignored.

        Args:
            regression_id: UUID of the row to update.
            **kwargs:      Field-value pairs to update.

        Returns:
            ``True`` if the row was found and at least one column updated,
            ``False`` if no matching row exists or no valid kwargs were given.
        """
        allowed = {"status", "diagnosis", "fix_run_id", "fix_attempt_count"}
        updates: List[str] = []
        values: List[Any] = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            values.append(value)
        if not updates:
            return False
        values.append(regression_id)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE regressions SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            return cursor.rowcount > 0

    def list_regressions(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List regression records, newest first.

        Args:
            status: Optional status filter (e.g. ``'detected'``, ``'fixed'``).
            limit:  Maximum rows to return (default ``100``).
            offset: Rows to skip for pagination (default ``0``).

        Returns:
            List of regression dicts ordered by ``created_at DESC``.
            ``affected_files`` is deserialised to a Python list.
        """
        query = "SELECT * FROM regressions WHERE 1=1"
        params: List[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # CI Green SHA Tracking (Issue #3.3a.3)
    # ------------------------------------------------------------------

    def store_green_sha(self, repo_slug: str, sha: str) -> None:
        """Upsert the last-known-green CI SHA for a repository.

        Uses an INSERT OR REPLACE so this is safe to call on first write
        (insert) and on every subsequent CI pass (update).

        Args:
            repo_slug: Repository identifier in ``owner/repo`` format.
            sha:       The green commit SHA to persist.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO ci_green_shas (repo_slug, sha, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_slug) DO UPDATE SET
                    sha        = excluded.sha,
                    updated_at = excluded.updated_at
                """,
                (repo_slug, sha, now),
            )

    def get_last_green_sha(self, repo_slug: str) -> Optional[str]:
        """Return the most recent green CI SHA for a repository, or None.

        Args:
            repo_slug: Repository identifier in ``owner/repo`` format.

        Returns:
            The green SHA string, or ``None`` if no record exists yet.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT sha FROM ci_green_shas WHERE repo_slug = ?",
                (repo_slug,),
            )
            row = cursor.fetchone()
        return row["sha"] if row else None

    # ------------------------------------------------------------------
    # Review Outcome Operations (Issue #4.1.2)
    # ------------------------------------------------------------------

    def insert_review_outcome(self, data: Dict[str, Any]) -> int:
        """Insert a review outcome record and return the rowid.

        Args:
            data: Dict with keys matching the ``review_outcomes`` table columns:
                - ``review_id`` (str): UUID primary key.
                - ``run_id`` (str): Pipeline run identifier.
                - ``phase_id`` (str): Phase identifier (e.g. ``"review"``).
                - ``reviewer_model`` (str, optional): Model tier/name.
                - ``verdict`` (str, optional): ``"APPROVE"`` or ``"REQUEST_CHANGES"``.
                - ``issues_found`` (list): List of issue dicts — serialised to
                  JSON by this method.
                - ``fix_verified`` (bool, optional): Defaults to ``False``.
                - ``created_at`` (str, optional): ISO-8601 timestamp; defaults
                  to the current DB timestamp when omitted.

        Returns:
            The ``rowid`` of the newly inserted row (integer).
        """
        issues_json = json.dumps(data.get("issues_found", []))
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO review_outcomes
                    (review_id, run_id, phase_id, reviewer_model,
                     verdict, issues_found, fix_verified, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["review_id"],
                    data["run_id"],
                    data["phase_id"],
                    data.get("reviewer_model"),
                    data.get("verdict"),
                    issues_json,
                    int(bool(data.get("fix_verified") or False)),
                    data.get("created_at"),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_review_outcomes_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        """Return all review outcome rows for a given pipeline run.

        Rows are ordered by ``created_at ASC`` so the caller sees outcomes
        in chronological order (relevant when a run has multiple review
        phases).

        Args:
            run_id: The pipeline run identifier to look up.

        Returns:
            List of review outcome dicts (``issues_found`` deserialised to a
            Python list).  Returns an empty list when no outcomes exist.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM review_outcomes
                WHERE run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_review_outcomes(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return a paginated global listing of all review outcomes.

        Rows are ordered by ``created_at DESC`` (newest first).  Use
        ``limit`` and ``offset`` for cursor-based pagination.

        Args:
            limit:  Maximum number of rows to return (default ``50``).
            offset: Number of rows to skip for pagination (default ``0``).

        Returns:
            List of review outcome dicts ordered by ``created_at DESC``.
            ``issues_found`` is deserialised to a Python list.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM review_outcomes
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]
