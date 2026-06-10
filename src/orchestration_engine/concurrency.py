"""Concurrency Manager for the Orchestration Engine.

Manages worker pools, resource limits, and thread-safe task assignment.
Handles worker lifecycle, heartbeat tracking, and stale worker detection.
"""

# Trailing whitespace below lives inside multi-line string literals;
# ruff only offers --unsafe-fixes (string-byte edits) for it.
# ruff: noqa: W291

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4

from .config import EngineConfig
from .db import Database
from .timestamps import now_utc

logger = logging.getLogger(__name__)


class WorkerState(str, Enum):
    """Worker lifecycle states."""

    IDLE = "idle"  # Available for tasks
    ASSIGNED = "assigned"  # Task assigned, not yet started
    RUNNING = "running"  # Currently executing task
    STALE = "stale"  # No heartbeat, presumed dead
    TERMINATED = "terminated"  # Explicitly terminated


@dataclass
class WorkerInfo:
    """Information about a worker."""

    worker_id: str
    state: WorkerState
    assigned_task_id: Optional[str] = None
    session_id: Optional[str] = None
    created_at: datetime = None
    last_heartbeat: datetime = None
    last_activity: Optional[str] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = now_utc()
        if self.last_heartbeat is None:
            self.last_heartbeat = now_utc()


class ResourceLimits:
    """Track and enforce resource limits."""

    def __init__(self, config: EngineConfig):
        self.config = config
        self._lock = threading.Lock()
        self._current_sessions = 0
        self._daily_cost_usd = 0.0
        self._last_reset = now_utc().date()

    def check_session_limit(self) -> bool:
        """Check if we can create a new session."""
        with self._lock:
            return self._current_sessions < self.config.resources.max_concurrent_sessions

    def acquire_session(self) -> bool:
        """Acquire a session slot."""
        with self._lock:
            if self._current_sessions >= self.config.resources.max_concurrent_sessions:
                return False

            self._current_sessions += 1
            logger.debug(
                f"Session acquired. Current: {self._current_sessions}/{self.config.resources.max_concurrent_sessions}"  # noqa: E501
            )
            return True

    def release_session(self) -> None:
        """Release a session slot."""
        with self._lock:
            if self._current_sessions > 0:
                self._current_sessions -= 1
                logger.debug(
                    f"Session released. Current: {self._current_sessions}/{self.config.resources.max_concurrent_sessions}"  # noqa: E501
                )

    def check_daily_budget(self, estimated_cost_usd: float = 0.0) -> bool:
        """Check if task would exceed daily budget."""
        with self._lock:
            # Reset daily counter if new day
            today = now_utc().date()
            if today != self._last_reset:
                self._daily_cost_usd = 0.0
                self._last_reset = today

            if self.config.resources.daily_budget_usd is None:
                return True  # No budget limit set

            return (self._daily_cost_usd + estimated_cost_usd) <= float(
                self.config.resources.daily_budget_usd
            )

    def record_cost(self, cost_usd: float) -> None:
        """Record cost against daily budget."""
        with self._lock:
            self._daily_cost_usd += cost_usd
            logger.debug(
                f"Recorded cost: ${cost_usd:.4f}. Daily total: ${self._daily_cost_usd:.4f}"
            )

    def get_status(self) -> Dict[str, Any]:
        """Get current resource usage status."""
        with self._lock:
            return {
                "current_sessions": self._current_sessions,
                "max_sessions": self.config.resources.max_concurrent_sessions,
                "session_utilization": (
                    self._current_sessions / self.config.resources.max_concurrent_sessions
                )
                * 100,
                "daily_cost_usd": self._daily_cost_usd,
                "daily_budget_usd": (
                    float(self.config.resources.daily_budget_usd)
                    if self.config.resources.daily_budget_usd
                    else None
                ),
                "budget_utilization": (
                    (self._daily_cost_usd / float(self.config.resources.daily_budget_usd) * 100)
                    if self.config.resources.daily_budget_usd
                    else 0
                ),
            }


class WorkerPool:
    """Thread-safe worker pool with resource management."""

    def __init__(self, database: Database, config: EngineConfig):
        """Initialize the worker pool.

        Args:
            database: Database instance for persistence
            config: Engine configuration
        """
        self.db = database
        self.config = config
        self.resources = ResourceLimits(config)

        # Thread-safe data structures
        self._lock = threading.RLock()
        self._workers: Dict[str, WorkerInfo] = {}
        self._task_assignments: Dict[str, str] = {}  # task_id -> worker_id

        # Background threads
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._cleanup_thread: Optional[threading.Thread] = None
        self._running = False

        self._init_tables()

    def _init_tables(self) -> None:
        """Initialize worker tracking tables."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                assigned_task_id TEXT,
                session_id TEXT,
                created_at TIMESTAMP NOT NULL,
                last_heartbeat TIMESTAMP NOT NULL,
                last_activity TEXT
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_state ON workers(state)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_worker_heartbeat ON workers(last_heartbeat)"
        )

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS resource_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metric_name TEXT NOT NULL,
                metric_value REAL NOT NULL,
                details TEXT
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_resource_time ON resource_usage(timestamp)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_resource_metric ON resource_usage(metric_name)"
        )

    def start(self) -> None:
        """Start the worker pool background threads."""
        with self._lock:
            if self._running:
                return

            self._running = True

            # Start heartbeat monitoring thread
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_monitor, name="WorkerPool-Heartbeat", daemon=True
            )
            self._heartbeat_thread.start()

            # Start cleanup thread
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_monitor, name="WorkerPool-Cleanup", daemon=True
            )
            self._cleanup_thread.start()

            logger.info(f"Worker pool started with max_workers={self.config.queue.max_workers}")

    def stop(self) -> None:
        """Stop the worker pool and cleanup threads."""
        with self._lock:
            if not self._running:
                return

            self._running = False

            # Terminate all workers
            for worker_id in list(self._workers.keys()):
                self.terminate_worker(worker_id, reason="Pool shutdown")

            logger.info("Worker pool stopped")

    def create_worker(self) -> Optional[str]:
        """Create a new worker if resources allow.

        Returns:
            Worker ID if created, None if resource limits prevent creation
        """
        with self._lock:
            # Check worker count limit
            active_workers = len(
                [
                    w
                    for w in self._workers.values()
                    if w.state not in [WorkerState.TERMINATED, WorkerState.STALE]
                ]
            )

            if active_workers >= self.config.queue.max_workers:
                logger.debug(
                    f"Cannot create worker: at limit ({active_workers}/{self.config.queue.max_workers})"  # noqa: E501
                )
                return None

            # Create worker
            worker_id = f"worker-{uuid4().hex[:8]}"
            worker = WorkerInfo(worker_id=worker_id, state=WorkerState.IDLE)

            self._workers[worker_id] = worker

            # Persist to database
            self.db.execute(
                """
                INSERT INTO workers (worker_id, state, created_at, last_heartbeat)
                VALUES (?, ?, ?, ?)
            """,
                [worker_id, worker.state.value, worker.created_at, worker.last_heartbeat],
            )

            logger.info(f"Created worker {worker_id}")
            return worker_id

    def assign_task(self, task_id: str) -> Optional[str]:
        """Assign a task to an available worker.

        Creates a new worker for each task (or reuses one from the pool when
        at the worker limit).  Workers are terminated after task completion so
        the pool stays within bounds.

        Args:
            task_id: Task to assign

        Returns:
            Worker ID if assigned, None if no workers available
        """
        with self._lock:
            # Try to create a new dedicated worker for this task
            worker_id = self.create_worker()
            if worker_id is None:
                # At the worker limit — try to reuse an idle worker
                idle_workers = [w for w in self._workers.values() if w.state == WorkerState.IDLE]
                if not idle_workers:
                    return None
                worker = idle_workers[0]
                worker_id = worker.worker_id
            worker = self._workers[worker_id]

            # Assign task
            worker.state = WorkerState.ASSIGNED
            worker.assigned_task_id = task_id
            worker.last_activity = f"Assigned task {task_id}"

            self._task_assignments[task_id] = worker_id

            # Update database
            self.db.execute(
                """
                UPDATE workers 
                SET state = ?, assigned_task_id = ?, last_activity = ?
                WHERE worker_id = ?
            """,
                [worker.state.value, task_id, worker.last_activity, worker_id],
            )

            logger.info(f"Assigned task {task_id} to worker {worker_id}")
            return worker_id

    def start_task_execution(self, worker_id: str, session_id: str = None) -> None:
        """Mark worker as running task execution.

        Args:
            worker_id: Worker starting execution
            session_id: OpenClaw session ID if applicable
        """
        with self._lock:
            worker = self._workers.get(worker_id)
            if not worker or worker.state != WorkerState.ASSIGNED:
                logger.warning(f"Cannot start execution for worker {worker_id}: invalid state")
                return

            worker.state = WorkerState.RUNNING
            worker.session_id = session_id
            worker.last_activity = f"Started executing task {worker.assigned_task_id}"
            worker.last_heartbeat = now_utc()

            # Acquire session resource
            if session_id:
                self.resources.acquire_session()

            # Update database
            self.db.execute(
                """
                UPDATE workers 
                SET state = ?, session_id = ?, last_activity = ?, last_heartbeat = ?
                WHERE worker_id = ?
            """,
                [
                    worker.state.value,
                    session_id,
                    worker.last_activity,
                    worker.last_heartbeat,
                    worker_id,
                ],
            )

            logger.info(f"Worker {worker_id} started task execution (session: {session_id})")

    def complete_task(self, worker_id: str, success: bool = True, cost_usd: float = None) -> None:
        """Mark task completion and free worker.

        Args:
            worker_id: Worker completing task
            success: Whether task completed successfully
            cost_usd: Task execution cost
        """
        with self._lock:
            worker = self._workers.get(worker_id)
            if not worker:
                logger.warning(f"Worker {worker_id} not found")
                return

            task_id = worker.assigned_task_id
            session_id = worker.session_id

            # Record cost if provided
            if cost_usd:
                self.resources.record_cost(cost_usd)

            # Release resources
            if session_id:
                self.resources.release_session()

            # Free worker
            worker.state = WorkerState.IDLE
            worker.assigned_task_id = None
            worker.session_id = None
            worker.last_activity = (
                f"Completed task {task_id} ({'success' if success else 'failed'})"
            )
            worker.last_heartbeat = now_utc()

            # Remove task assignment
            if task_id and task_id in self._task_assignments:
                del self._task_assignments[task_id]

            # Update database
            self.db.execute(
                """
                UPDATE workers 
                SET state = ?, assigned_task_id = NULL, session_id = NULL, 
                    last_activity = ?, last_heartbeat = ?
                WHERE worker_id = ?
            """,
                [worker.state.value, worker.last_activity, worker.last_heartbeat, worker_id],
            )

            logger.info(f"Worker {worker_id} completed task {task_id}")

    def heartbeat(self, worker_id: str, activity: str = None) -> bool:
        """Update worker heartbeat.

        Args:
            worker_id: Worker sending heartbeat
            activity: Optional activity description

        Returns:
            True if heartbeat accepted, False if worker not found
        """
        with self._lock:
            worker = self._workers.get(worker_id)
            if not worker or worker.state == WorkerState.TERMINATED:
                return False

            worker.last_heartbeat = now_utc()
            if activity:
                worker.last_activity = activity

            # Update database
            self.db.execute(
                """
                UPDATE workers 
                SET last_heartbeat = ?, last_activity = COALESCE(?, last_activity)
                WHERE worker_id = ?
            """,
                [worker.last_heartbeat, activity, worker_id],
            )

            return True

    def terminate_worker(self, worker_id: str, reason: str = None) -> bool:
        """Terminate a worker.

        Args:
            worker_id: Worker to terminate
            reason: Termination reason

        Returns:
            True if worker was terminated, False if not found
        """
        with self._lock:
            worker = self._workers.get(worker_id)
            if not worker:
                return False

            task_id = worker.assigned_task_id
            session_id = worker.session_id

            # Release resources
            if session_id:
                self.resources.release_session()

            # Remove task assignment
            if task_id and task_id in self._task_assignments:
                del self._task_assignments[task_id]

            # Mark as terminated
            worker.state = WorkerState.TERMINATED
            worker.last_activity = f"Terminated: {reason or 'Manual termination'}"
            worker.last_heartbeat = now_utc()

            # Update database
            self.db.execute(
                """
                UPDATE workers 
                SET state = ?, assigned_task_id = NULL, session_id = NULL,
                    last_activity = ?, last_heartbeat = ?
                WHERE worker_id = ?
            """,
                [worker.state.value, worker.last_activity, worker.last_heartbeat, worker_id],
            )

            logger.info(f"Terminated worker {worker_id}: {reason}")
            return True

    def get_worker_status(self, worker_id: str = None) -> Dict[str, Any]:
        """Get worker status information.

        Args:
            worker_id: Specific worker ID, or None for all workers

        Returns:
            Worker status dictionary
        """
        with self._lock:
            if worker_id:
                worker = self._workers.get(worker_id)
                if not worker:
                    return {}

                return {
                    "worker_id": worker.worker_id,
                    "state": worker.state.value,
                    "assigned_task_id": worker.assigned_task_id,
                    "session_id": worker.session_id,
                    "created_at": worker.created_at.isoformat(),
                    "last_heartbeat": worker.last_heartbeat.isoformat(),
                    "last_activity": worker.last_activity,
                    "heartbeat_age_seconds": (now_utc() - worker.last_heartbeat).total_seconds(),
                }
            else:
                # Return summary of all workers
                workers_by_state = {}
                for worker in self._workers.values():
                    state = worker.state.value
                    if state not in workers_by_state:
                        workers_by_state[state] = []
                    workers_by_state[state].append(
                        {
                            "worker_id": worker.worker_id,
                            "assigned_task_id": worker.assigned_task_id,
                            "session_id": worker.session_id,
                            "heartbeat_age_seconds": (
                                now_utc() - worker.last_heartbeat
                            ).total_seconds(),
                        }
                    )

                return {
                    "total_workers": len(self._workers),
                    "workers_by_state": workers_by_state,
                    "resource_status": self.resources.get_status(),
                    "max_workers": self.config.queue.max_workers,
                }

    def get_task_worker(self, task_id: str) -> Optional[str]:
        """Get worker ID assigned to a task.

        Args:
            task_id: Task identifier

        Returns:
            Worker ID or None if task not assigned
        """
        with self._lock:
            return self._task_assignments.get(task_id)

    def _heartbeat_monitor(self) -> None:
        """Background thread to monitor worker heartbeats."""
        stale_timeout = timedelta(minutes=self.config.queue.stale_worker_timeout_minutes)

        while self._running:
            try:
                now = now_utc()
                stale_workers = []

                with self._lock:
                    for worker in self._workers.values():
                        if (
                            worker.state in [WorkerState.RUNNING, WorkerState.ASSIGNED]
                            and now - worker.last_heartbeat > stale_timeout
                        ):
                            stale_workers.append(worker.worker_id)  # noqa: PERF401

                # Mark stale workers (outside lock to avoid deadlock)
                for worker_id in stale_workers:
                    self._mark_worker_stale(worker_id)

                time.sleep(30)  # Check every 30 seconds

            except Exception as e:  # noqa: BLE001, PERF203
                logger.error(f"Error in heartbeat monitor: {e}")

    def _mark_worker_stale(self, worker_id: str) -> None:
        """Mark a worker as stale and release its resources."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if not worker:
                return

            task_id = worker.assigned_task_id
            session_id = worker.session_id

            logger.warning(
                f"Worker {worker_id} marked as stale (last heartbeat: {worker.last_heartbeat})"
            )

            # Release resources
            if session_id:
                self.resources.release_session()

            # Remove task assignment - task will be retried
            if task_id and task_id in self._task_assignments:
                del self._task_assignments[task_id]

            # Mark as stale
            worker.state = WorkerState.STALE
            worker.last_activity = f"Marked stale - no heartbeat since {worker.last_heartbeat}"

            # Update database
            self.db.execute(
                """
                UPDATE workers 
                SET state = ?, assigned_task_id = NULL, session_id = NULL, last_activity = ?
                WHERE worker_id = ?
            """,
                [worker.state.value, worker.last_activity, worker_id],
            )

    def _cleanup_monitor(self) -> None:
        """Background thread to clean up old workers."""
        cleanup_age = timedelta(hours=24)  # Clean up workers older than 24 hours

        while self._running:
            try:
                cutoff = now_utc() - cleanup_age

                with self._lock:
                    to_remove = [
                        worker_id
                        for worker_id, worker in self._workers.items()
                        if worker.state in [WorkerState.TERMINATED, WorkerState.STALE]
                        and worker.last_heartbeat < cutoff
                    ]

                # Remove old workers
                for worker_id in to_remove:
                    with self._lock:
                        del self._workers[worker_id]

                    self.db.execute("DELETE FROM workers WHERE worker_id = ?", [worker_id])
                    logger.debug(f"Cleaned up old worker {worker_id}")

                time.sleep(3600)  # Run cleanup every hour

            except Exception as e:  # noqa: BLE001, PERF203
                logger.error(f"Error in cleanup monitor: {e}")

    def get_available_capacity(self) -> int:
        """Get number of tasks that can be assigned right now.

        Returns:
            Number of available worker slots
        """
        with self._lock:
            active_workers = len(
                [
                    w
                    for w in self._workers.values()
                    if w.state not in [WorkerState.TERMINATED, WorkerState.STALE]
                ]
            )
            idle_workers = len([w for w in self._workers.values() if w.state == WorkerState.IDLE])

            # Can create new workers up to max_workers limit
            can_create = max(0, self.config.queue.max_workers - active_workers)

            return idle_workers + can_create
