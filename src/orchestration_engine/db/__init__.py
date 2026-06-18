"""Database layer for the Orchestration Engine.

Provides SQLite-backed persistent storage with WAL mode, proper indexing,
connection management, and schema migrations.

EPIC #942 / sub-issue 951a: ``db.py`` is now the ``db/`` package. This module
is the *facade* — it re-exports the exact public surface the original module
exposed (``Database``, ``default_db_path``, ``parse_json_list``,
``TERMINAL_STATUSES``, ``STALE_TASK_THRESHOLD_MINUTES``) so no caller import
line changes anywhere. The connection/transaction core lives in
:mod:`._core` (``CoreMixin``); module constants + the sqlite3 adapter
registration live in :mod:`._consts`; schema DDL in :mod:`._schema`
(``SchemaMixin``); migrations in :mod:`._migrations` (``MigrationsMixin``);
the task / task-run domain in :mod:`._tasks` (``TasksMixin``, sub-issue 951c);
and the pipeline-run domain in :mod:`._pipeline_runs`
(``PipelineRunsMixin``, sub-issue 951c). The remaining ``Database`` methods
stay defined inline here and migrate to per-domain mixins in 951d-e.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

import json
import logging
from datetime import datetime, timezone
from pathlib import (
    Path,  # noqa: F401  # re-exported: tests patch db.Path.home for default_db_path()
)
from typing import Any, Dict, List, Optional

from ._consts import (  # noqa: F401  # re-exported public surface + sqlite adapter registration
    STALE_TASK_THRESHOLD_MINUTES,
    TERMINAL_STATUSES,
    default_db_path,
    parse_json_list,
)
from ._core import CoreMixin
from ._migrations import MigrationsMixin
from ._pipeline_runs import PipelineRunsMixin
from ._schema import SchemaMixin
from ._tasks import TasksMixin

logger = logging.getLogger(__name__)

from ..timestamps import normalize_ts, now_utc  # noqa: E402


class Database(CoreMixin, SchemaMixin, MigrationsMixin, TasksMixin, PipelineRunsMixin):
    """SQLite database manager with connection pooling and migrations."""

    # Orchestra Operations

    def insert_orchestra(self, orchestra_data: Dict[str, Any]) -> str:
        """Insert a new orchestra workflow."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO orchestras (
                    id, template, name, status, config, priority,
                    cost_budget_usd, time_budget_hours, created_by, tags
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """,
                (
                    orchestra_data["id"],
                    orchestra_data["template"],
                    orchestra_data.get("name"),
                    orchestra_data.get("status", "running"),
                    json.dumps(orchestra_data["config"]),
                    orchestra_data.get("priority", 3),
                    orchestra_data.get("cost_budget_usd"),
                    orchestra_data.get("time_budget_hours"),
                    orchestra_data.get("created_by"),
                    json.dumps(orchestra_data.get("tags", [])),
                ),
            )

        return orchestra_data["id"]

    def get_orchestra(self, orchestra_id: str) -> Optional[Dict[str, Any]]:
        """Get orchestra by ID."""
        conn = self.get_connection()
        cursor = conn.execute("SELECT * FROM orchestras WHERE id = ?", (orchestra_id,))
        row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_dict(row)

    def update_orchestra_stats(self, orchestra_id: str) -> bool:
        """Update orchestra task counts based on current task states."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE orchestras 
                SET 
                    total_tasks = (
                        SELECT COUNT(*) FROM tasks WHERE orchestra_id = ?
                    ),
                    completed_tasks = (
                        SELECT COUNT(*) FROM tasks 
                        WHERE orchestra_id = ? AND status = 'success'
                    ),
                    failed_tasks = (
                        SELECT COUNT(*) FROM tasks 
                        WHERE orchestra_id = ? AND status IN ('failed', 'permanently_failed')
                    ),
                    cancelled_tasks = (
                        SELECT COUNT(*) FROM tasks 
                        WHERE orchestra_id = ? AND status = 'cancelled'
                    )
                WHERE id = ?
            """,
                (orchestra_id, orchestra_id, orchestra_id, orchestra_id, orchestra_id),
            )

            return cursor.rowcount > 0

    # Dead Letter Queue Operations

    def move_to_dead_letter(self, task_id: str, failure_reason: str) -> bool:
        """Move a permanently failed task to dead letter queue."""
        with self.transaction() as conn:
            # Get task data
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            task_row = cursor.fetchone()

            if task_row is None:
                return False

            # Insert into dead letter queue
            conn.execute(
                """
                INSERT INTO dead_letter_queue (
                    id, original_task_id, task_type, failure_reason,
                    failure_count, payload, error_patterns, suggested_fixes
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
            """,
                (
                    f"dl_{task_id}",
                    task_id,
                    task_row["type"],
                    failure_reason,
                    task_row["retry_count"],
                    task_row["payload"],
                    # Intentionally empty: move_to_dead_letter() has only a free-text
                    # failure_reason and performs no analysis. The namesake error_patterns
                    # table (recovery.py) is a separate frequency store and is not joined
                    # here, so no per-row source exists (#932).
                    json.dumps([]),
                    # Intentionally empty: suggested_fixes has no producer anywhere in the
                    # codebase. Populating it would be net-new analysis/suggestion work,
                    # out of scope (#932).
                    json.dumps([]),
                ),
            )

            # Update original task status
            conn.execute(
                """
                UPDATE tasks 
                SET status = 'permanently_failed', completed_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """,
                (task_id,),
            )

            return True

    # Statistics and Analytics

    def get_queue_stats(self) -> Dict[str, Any]:
        """Get comprehensive queue statistics."""
        conn = self.get_connection()

        # Basic counts by status
        cursor = conn.execute("""
            SELECT status, COUNT(*) as count
            FROM tasks 
            GROUP BY status
        """)
        status_counts = {row[0]: row[1] for row in cursor.fetchall()}

        # Priority breakdown
        cursor = conn.execute("""
            SELECT priority, COUNT(*) as count
            FROM tasks 
            WHERE status IN ('queued', 'running', 'retry')
            GROUP BY priority
        """)
        priority_counts = {f"priority_{row[0]}": row[1] for row in cursor.fetchall()}

        # Type breakdown
        cursor = conn.execute("""
            SELECT type, COUNT(*) as count
            FROM tasks 
            WHERE status IN ('queued', 'running', 'retry')
            GROUP BY type
        """)
        type_counts = {row[0]: row[1] for row in cursor.fetchall()}

        # Average execution time
        cursor = conn.execute("""
            SELECT AVG(
                (julianday(completed_at) - julianday(started_at)) * 86400
            ) as avg_seconds
            FROM tasks 
            WHERE started_at IS NOT NULL AND completed_at IS NOT NULL
        """)
        avg_execution_time = cursor.fetchone()[0]

        # Dead letter count
        cursor = conn.execute("SELECT COUNT(*) FROM dead_letter_queue")
        dead_letter_count = cursor.fetchone()[0]

        return {
            "timestamp": now_utc(),
            "queued": status_counts.get("queued", 0),
            "running": status_counts.get("running", 0),
            "completed": status_counts.get("success", 0),
            "failed": status_counts.get("failed", 0),
            "retrying": status_counts.get("retry", 0),
            "cancelled": status_counts.get("cancelled", 0),
            "priority_breakdown": priority_counts,
            "type_breakdown": type_counts,
            "avg_execution_time_seconds": avg_execution_time,
            "dead_letter_count": dead_letter_count,
            # Always 0 here: live worker count is runtime/process state with no DB
            # source. The sole consumer (queue.QueueManager.get_queue_stats) overrides
            # this with a heartbeat-based count, so this value is never surfaced to
            # users (#932).
            "active_workers": 0,
            "max_workers": 8,
        }

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

    def append_admin_audit(
        self,
        action: str,
        target: str,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        source_pid: Optional[int] = None,
    ) -> int:
        """Append a row to ``admin_audit_log``. Returns the new row id.

        Args:
            action: Short verb describing what changed (e.g.
                ``"update_feature_flags"``, ``"reset_admin_state"``).
            target: Which surface was mutated (e.g.
                ``"feature_flags"``, ``"autonomy_level"``, ``"modes"``).
                Multiple targets per action are concatenated comma-separated.
            before: Pre-mutation value (dict or None when first write).
            after: Post-mutation value.
            source_pid: OS pid of the FastAPI worker process. Default
                ``os.getpid()`` if not supplied.
        """
        import json as _json  # noqa: PLC0415
        import os as _os  # noqa: PLC0415

        pid = source_pid if source_pid is not None else _os.getpid()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO admin_audit_log
                    (action, target, before_json, after_json, source_pid)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    action,
                    target,
                    _json.dumps(before) if before is not None else None,
                    _json.dumps(after) if after is not None else None,
                    pid,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_admin_audit(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """Return up to ``limit`` recent admin audit rows, newest first.

        Each row is a dict with the columns of ``admin_audit_log``;
        ``before_json``/``after_json`` are parsed back into dicts (or None).
        """
        import json as _json  # noqa: PLC0415

        with self.transaction() as conn:
            cur = conn.execute(
                """
                SELECT id, action, target, before_json, after_json,
                       source_pid, created_at
                  FROM admin_audit_log
                 ORDER BY created_at DESC, id DESC
                 LIMIT ? OFFSET ?
                """,
                (int(limit), int(offset)),
            )
            rows: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                before = _json.loads(r["before_json"]) if r["before_json"] else None
                after = _json.loads(r["after_json"]) if r["after_json"] else None
                # Normalise created_at — SQLite returns a datetime object
                # when PARSE_DECLTYPES is set (see get_connection), but the
                # API surface is JSON so we need a string. ``normalize_ts``
                # (from ``.timestamps``) handles datetime -> isoformat and
                # Z-suffixes naive UTC strings so JS clients don't
                # misinterpret them as local time. (#876)
                created_str = normalize_ts(r["created_at"])
                rows.append(
                    {
                        "id": r["id"],
                        "action": r["action"],
                        "target": r["target"],
                        "before": before,
                        "after": after,
                        "source_pid": r["source_pid"],
                        "created_at": created_str,
                    }
                )
            return rows

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
