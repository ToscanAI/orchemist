"""Orchestra / dead-letter-queue / queue-stats domain mixin for
:class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951e) WITHOUT
behavioural change. Holds the orchestra-workflow CRUD/stats
(``insert_orchestra``, ``get_orchestra``, ``update_orchestra_stats``), the
dead-letter-queue mover (``move_to_dead_letter``) and the aggregate queue
statistics reader (``get_queue_stats``). Method bodies are byte-identical to
the original; only the import depth of intra-package references is adjusted.
These methods reference connection/transaction helpers (``self.transaction`` /
``self.get_connection`` / ``self._row_to_dict``) resolved at runtime via the MRO
from :class:`~orchestration_engine.db._core.CoreMixin`.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

import json
from typing import Any, Dict, Optional

from ..timestamps import now_utc


class OrchestraMixin:
    """Orchestra-workflow CRUD/stats, dead-letter-queue mover and queue stats.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`.
    """

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
