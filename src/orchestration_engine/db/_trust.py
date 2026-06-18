"""Trust-profile domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951e) WITHOUT
behavioural change. Holds the trust-profile CRUD plus trust-adjustment
event helpers (``upsert_trust_profile``, ``get_trust_profile``,
``insert_trust_adjustment``, ``list_trust_adjustments``, ``list_trust_profiles``,
``get_trust_profile_by_id``, Issue #4.2.1). Method bodies are byte-identical to
the original; only the import depth of intra-package references is adjusted.
These methods reference connection/transaction helpers (``self.transaction`` /
``self._locked`` / ``self.get_connection`` / ``self._row_to_dict``) resolved at
runtime via the MRO from
:class:`~orchestration_engine.db._core.CoreMixin`.

Trust methods remain ``mock.patch``-safe at
``orchestration_engine.db.Database.<name>``; db has a trivial single-chain MRO
and no ``super()``, so patching an inherited method resolves+restores cleanly.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class TrustMixin:
    """Trust-profile CRUD and trust-adjustment event operations.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # ------------------------------------------------------------------
    # Trust Profile CRUD (Issue #4.2.1)
    # ------------------------------------------------------------------

    def upsert_trust_profile(self, profile_data: Dict[str, Any]) -> int:
        """Insert or update a trust profile row and return the row id.

        Uses an ``INSERT … ON CONFLICT(repo, template_id, task_type) DO UPDATE``
        strategy so this is safe to call on both first write (insert) and
        subsequent updates.

        On conflict all mutable columns are overwritten with the supplied
        values; ``created_at`` is left unchanged (set only at initial insert).

        Args:
            profile_data: Dict matching the ``TrustProfile`` dataclass fields.
                          Required keys: ``repo``, ``template_id``,
                          ``task_type``.  Optional keys default to their DB
                          column defaults when omitted.

        Returns:
            The integer ``id`` (primary key) of the inserted or updated row.
        """
        now = profile_data.get("updated_at") or datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trust_profiles
                    (repo, template_id, task_type,
                     auto_merge_threshold, human_review_threshold,
                     trust_score, total_runs, successful_merges,
                     regressions, reverted_prs, last_run_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, template_id, task_type) DO UPDATE SET
                    auto_merge_threshold   = excluded.auto_merge_threshold,
                    human_review_threshold = excluded.human_review_threshold,
                    trust_score            = excluded.trust_score,
                    total_runs             = excluded.total_runs,
                    successful_merges      = excluded.successful_merges,
                    regressions            = excluded.regressions,
                    reverted_prs           = excluded.reverted_prs,
                    last_run_at            = excluded.last_run_at,
                    updated_at             = excluded.updated_at
                """,
                (
                    profile_data["repo"],
                    profile_data["template_id"],
                    profile_data["task_type"],
                    float(profile_data.get("auto_merge_threshold", 0.85)),
                    float(profile_data.get("human_review_threshold", 0.70)),
                    float(profile_data.get("trust_score", 0.5)),
                    int(profile_data.get("total_runs", 0)),
                    int(profile_data.get("successful_merges", 0)),
                    int(profile_data.get("regressions", 0)),
                    int(profile_data.get("reverted_prs", 0)),
                    profile_data.get("last_run_at"),
                    profile_data.get("created_at") or now,
                    now,
                ),
            )
            # lastrowid works for both INSERT and the DO UPDATE path in SQLite ≥ 3.35
            rowid = cursor.lastrowid
            if rowid is None:
                # Fallback: fetch the id via the unique composite key
                row = conn.execute(
                    "SELECT id FROM trust_profiles WHERE repo=? AND template_id=? AND task_type=?",
                    (profile_data["repo"], profile_data["template_id"], profile_data["task_type"]),
                ).fetchone()
                rowid = row[0] if row else None
        return rowid  # type: ignore[return-value]

    def get_trust_profile(
        self,
        repo: str,
        template_id: str,
        task_type: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the trust profile for a (repo, template_id, task_type) triplet.

        Args:
            repo:        Git repository slug (e.g. ``"owner/repo"``).
            template_id: Pipeline template identifier.
            task_type:   Task type string (e.g. ``"bugfix"``).

        Returns:
            Dict with all ``trust_profiles`` columns, or ``None`` when no
            matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM trust_profiles
                WHERE repo = ? AND template_id = ? AND task_type = ?
                """,
                (repo, template_id, task_type),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def insert_trust_adjustment(self, adjustment_data: Dict[str, Any]) -> int:
        """Insert a trust adjustment event and return the new row id.

        Args:
            adjustment_data: Dict matching the ``trust_adjustments`` table
                             columns.  Required keys: ``profile_id``,
                             ``delta``, ``reason``, ``score_before``,
                             ``score_after``.  Optional: ``run_id``,
                             ``created_at``.

        Returns:
            The ``id`` (integer primary key) of the newly inserted row.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trust_adjustments
                    (profile_id, delta, reason, run_id, score_before, score_after, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(adjustment_data["profile_id"]),
                    float(adjustment_data["delta"]),
                    adjustment_data["reason"],
                    adjustment_data.get("run_id"),
                    float(adjustment_data["score_before"]),
                    float(adjustment_data["score_after"]),
                    adjustment_data.get("created_at") or datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def list_trust_adjustments(
        self,
        profile_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return trust adjustment events for a profile, newest first.

        Args:
            profile_id: Primary key of the parent ``trust_profiles`` row.
            limit:      Maximum rows to return (default ``100``).
            offset:     Rows to skip for pagination (default ``0``).

        Returns:
            List of adjustment dicts ordered by ``created_at DESC``.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM trust_adjustments
                WHERE profile_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (profile_id, limit, offset),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_trust_profiles(self) -> List[Dict[str, Any]]:
        """Return all trust profile rows, ordered by id ASC.

        Returns:
            List of dicts, one per ``trust_profiles`` row.  Empty list when no
            profiles have been created yet.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute("SELECT * FROM trust_profiles ORDER BY id ASC")
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_trust_profile_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        """Return a single trust profile row by its integer primary key.

        Args:
            profile_id: Integer primary key of the ``trust_profiles`` row.

        Returns:
            Dict with all ``trust_profiles`` columns, or ``None`` when no row
            matches the given ``profile_id``.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM trust_profiles WHERE id = ?",
                (profile_id,),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None
