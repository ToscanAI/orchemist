"""Task / task-run domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951c) WITHOUT
behavioural change. Holds the task lifecycle CRUD (``insert_task``,
``get_task``, ``update_task_status``, ``get_next_task``, ``list_tasks``,
``cancel_task``), the ``task_runs`` writers (``insert_task_run``,
``update_task_run``) and the ``task_runs`` aggregation readers
(``get_total_tokens_consumed`` .. ``has_stale_running_tasks``, Issue #932).
Method bodies are byte-identical to the original; only the import depth of
intra-package references is adjusted. These methods reference
connection/transaction helpers (``self.transaction`` / ``self._locked`` /
``self.fetch_one`` / ``self.fetch_all`` / ``self._row_to_dict``) resolved at
runtime via the MRO from
:class:`~orchestration_engine.db._core.CoreMixin`.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..timestamps import now_utc
from ._consts import STALE_TASK_THRESHOLD_MINUTES


class TasksMixin:
    """Task lifecycle, task_runs CRUD, and queue-health aggregation readers.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``fetch_one``,
    ``fetch_all``, ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

    # Task Operations

    def insert_task(self, task_data: Dict[str, Any]) -> str:
        """Insert a new task into the database."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, type, priority, status, payload, max_retries,
                    orchestra_id, orchestra_phase, min_confidence, preferred_model,
                    timeout_seconds, cost_limit_usd, created_by, tags, metadata
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """,
                (
                    task_data["id"],
                    task_data["type"],
                    task_data.get("priority", 3),
                    task_data.get("status", "queued"),
                    json.dumps(task_data["payload"]),
                    task_data.get("max_retries", 3),
                    task_data.get("orchestra_id"),
                    task_data.get("orchestra_phase"),
                    task_data.get("min_confidence", 0.7),
                    task_data.get("preferred_model"),
                    task_data.get("timeout_seconds", 3600),
                    task_data.get("cost_limit_usd"),
                    task_data.get("created_by"),
                    json.dumps(task_data.get("tags", [])),
                    json.dumps(task_data.get("metadata", {})),
                ),
            )

        return task_data["id"]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get a task by ID."""
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_dict(row)

    def update_task_status(self, task_id: str, status: str, **kwargs) -> bool:
        """Update task status and related fields."""
        updates = ["status = ?"]
        values = [status]

        # Handle status-specific updates
        if status == "running" and "started_at" not in kwargs:
            kwargs["started_at"] = now_utc()
        elif status in ["success", "failed", "permanently_failed"] and "completed_at" not in kwargs:
            kwargs["completed_at"] = now_utc()

        # Add additional updates
        for key, value in kwargs.items():
            if key in ["started_at", "completed_at", "next_retry_at"]:
                updates.append(f"{key} = ?")
                values.append(value)
            elif key == "retry_count":
                updates.append("retry_count = retry_count + 1")
            elif key == "metadata":
                updates.append("metadata = ?")
                values.append(json.dumps(value, default=str))

        values.append(task_id)

        with self.transaction() as conn:
            cursor = conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
            return cursor.rowcount > 0

    def get_next_task(self, worker_id: str) -> Optional[Dict[str, Any]]:  # noqa: ARG002
        """Get the next available task for execution."""
        with self.transaction() as conn:
            # Find next task using priority and retry logic
            cursor = conn.execute("""
                SELECT * FROM tasks 
                WHERE (status = 'queued' OR (status = 'retry' AND next_retry_at <= CURRENT_TIMESTAMP))
                ORDER BY 
                    CASE 
                        WHEN status = 'retry' THEN priority - 0.5 
                        ELSE priority 
                    END ASC,
                    created_at ASC
                LIMIT 1
            """)

            row = cursor.fetchone()
            if row is None:
                return None

            # Mark task as running
            task_id = row["id"]
            conn.execute(
                """
                UPDATE tasks 
                SET status = 'running', started_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """,
                (task_id,),
            )

            return self._row_to_dict(row)

    def list_tasks(
        self,
        states: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        orchestra_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List tasks with optional filtering."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []

        if states:
            placeholders = ",".join("?" * len(states))
            query += f" AND status IN ({placeholders})"
            params.extend(states)

        if types:
            placeholders = ",".join("?" * len(types))
            query += f" AND type IN ({placeholders})"
            params.extend(types)

        if orchestra_id:
            query += " AND orchestra_id = ?"
            params.append(orchestra_id)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        conn = self.get_connection()
        cursor = conn.execute(query, params)

        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a queued or running task."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks 
                SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP 
                WHERE id = ? AND status IN ('queued', 'running', 'retry')
            """,
                (task_id,),
            )

            return cursor.rowcount > 0

    # Task Run Operations

    def insert_task_run(self, run_data: Dict[str, Any]) -> str:
        """Insert a new task run record."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO task_runs (
                    id, task_id, attempt_number, model, thinking_level,
                    session_id, worker_id, status, result, confidence,
                    error_message, error_type, tokens_used, cost_usd, peak_memory_mb
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """,
                (
                    run_data["id"],
                    run_data["task_id"],
                    run_data["attempt_number"],
                    run_data["model"],
                    run_data.get("thinking_level"),
                    run_data.get("session_id"),
                    run_data.get("worker_id"),
                    run_data["status"],
                    json.dumps(run_data.get("result")) if run_data.get("result") else None,
                    run_data.get("confidence"),
                    run_data.get("error_message"),
                    run_data.get("error_type"),
                    run_data.get("tokens_used", 0),
                    run_data.get("cost_usd"),
                    run_data.get("peak_memory_mb"),
                ),
            )

        return run_data["id"]

    def update_task_run(self, run_id: str, **kwargs) -> bool:
        """Update task run with completion data."""
        updates = []
        values = []

        for key, value in kwargs.items():
            if key == "result":
                updates.append("result = ?")
                values.append(json.dumps(value) if value else None)
            elif key in [
                "completed_at",
                "status",
                "confidence",
                "error_message",
                "error_type",
                "tokens_used",
                "cost_usd",
                "peak_memory_mb",
            ]:
                updates.append(f"{key} = ?")
                values.append(value)

        if not updates:
            return False

        values.append(run_id)

        with self.transaction() as conn:
            cursor = conn.execute(f"UPDATE task_runs SET {', '.join(updates)} WHERE id = ?", values)
            return cursor.rowcount > 0

    # Task Run Aggregation Readers (Issue #932 item 1)
    #
    # These roll up the EXISTING task_runs rows (and, for staleness, the tasks
    # table) for the queue-health surface in queue.py. They author the SQL the
    # data layer previously lacked (task_runs had writers but no aggregation
    # readers). Every aggregate column is aliased so _row_to_dict keys it
    # addressably; every SUM is COALESCE-guarded so empty/all-NULL data yields
    # a clean zero instead of NULL/raise.

    def get_total_tokens_consumed(self) -> int:
        """Total tokens used across all task_runs rows (all time).

        Sums task_runs.tokens_used over every attempt record. Returns 0 when
        there are no rows or all values are NULL (COALESCE guard).
        """
        row = self.fetch_one("SELECT COALESCE(SUM(tokens_used), 0) AS total FROM task_runs")
        return int(row["total"]) if row else 0

    def get_task_tokens_consumed(self, task_id: str) -> int:
        """Total tokens used across all attempts (task_runs) for one task.

        Sums task_runs.tokens_used WHERE task_id = ?. Returns 0 for an unknown
        task id or when all matching values are NULL.
        """
        row = self.fetch_one(
            "SELECT COALESCE(SUM(tokens_used), 0) AS total " "FROM task_runs WHERE task_id = ?",
            (task_id,),
        )
        return int(row["total"]) if row else 0

    def get_total_cost_today(self) -> Decimal:
        """Sum of task_runs.cost_usd for runs completed today (UTC).

        Cost is realized at attempt completion, so only rows with a non-NULL
        completed_at on today's UTC calendar date contribute. Returns
        Decimal('0.00') when no matching rows exist.
        """
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.get_total_cost_for_date(today_str)

    def get_total_cost_for_date(self, date_str: str) -> Decimal:
        """Sum of task_runs.cost_usd for runs whose completed_at date == date_str (UTC).

        SQLite DATE() extracts the calendar date regardless of whether
        completed_at was stored space-separated (CURRENT_TIMESTAMP) or
        T-separated (datetime.isoformat()), so the comparison is format-robust.

        The summation is performed in Decimal space rather than via SQL SUM():
        SQLite's DECIMAL(10,4) column has NUMERIC affinity backed by IEEE-754
        floats, so an in-engine SUM(cost_usd) drifts (0.10 + 0.20 ->
        0.30000000000000004). Pulling each value and folding it through
        Decimal(str(value)) yields the exact, drift-free total the Decimal
        contract requires.

        Args:
            date_str: 'YYYY-MM-DD' (UTC).

        Returns:
            Decimal sum of matching cost_usd, or Decimal('0.00') if none.
        """
        rows = self.fetch_all(
            "SELECT cost_usd FROM task_runs "
            "WHERE DATE(completed_at) = ? AND cost_usd IS NOT NULL",
            (date_str,),
        )
        total = Decimal("0")
        for row in rows:
            # str() first, never Decimal(float): str(0.1) is '0.1', whereas
            # Decimal(0.1) would be 0.1000000000000000055...
            total += Decimal(str(row["cost_usd"]))
        return total

    def has_stale_running_tasks(
        self, threshold_minutes: int = STALE_TASK_THRESHOLD_MINUTES
    ) -> bool:
        """True iff any task has been in 'running' state longer than threshold_minutes.

        Uses julianday() on BOTH sides so the comparison is correct regardless
        of whether tasks.started_at was written as SQLite CURRENT_TIMESTAMP
        (UTC, space-separated) or as a Python datetime.now().isoformat()
        (T-separated) — a raw string '<' is wrong because 'T' (0x54) sorts above
        ' ' (0x20), silently missing T-form stale rows. Compares against
        SQLite's own clock ('now', UTC), matching CURRENT_TIMESTAMP. Strict '<'
        on started_at means a task running exactly threshold_minutes is NOT
        stale; one running threshold_minutes + a moment IS.
        """
        row = self.fetch_one(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' "
            "AND started_at IS NOT NULL "
            "AND julianday(started_at) < julianday('now', ?)",
            (f"-{int(threshold_minutes)} minutes",),
        )
        return bool(row["n"]) if row else False
