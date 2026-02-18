"""Task Queue implementation for the Orchestration Engine.

Provides high-level task queue operations with retry logic, state management,
and worker coordination on top of the SQLite database layer.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any
from uuid import uuid4

from .db import Database
from .schemas import (
    TaskSpec, TaskStatus, TaskResult, TaskSummary, QueueStats,
    TaskState, Priority, TaskType, TaskFilters, TaskRunResult,
    generate_task_id, generate_orchestra_id, calculate_retry_delay,
    select_model_tier, DEFAULT_MAX_RETRIES, DeadLetterTask
)

logger = logging.getLogger(__name__)


class TaskQueue:
    """High-level task queue interface with retry logic and state management."""
    
    def __init__(self, database: Optional[Database] = None):
        """Initialize task queue.
        
        Args:
            database: Database instance. Creates new one if None.
        """
        self.db = database or Database()
        self._worker_heartbeats: Dict[str, datetime] = {}
    
    def submit_task(self, task_spec: TaskSpec) -> str:
        """Submit a new task to the queue.
        
        Args:
            task_spec: Task specification with type, payload, and options
            
        Returns:
            str: Unique task ID
            
        Raises:
            ValueError: If task specification is invalid
        """
        # Generate unique task ID
        task_id = generate_task_id()
        
        # Set default max retries based on task type if not specified
        max_retries = task_spec.max_retries
        if max_retries == 3:  # Default value, check for task-specific default
            max_retries = DEFAULT_MAX_RETRIES.get(task_spec.type, 3)
        
        # Prepare task data for database
        task_data = {
            'id': task_id,
            'type': task_spec.type.value,
            'priority': task_spec.priority.value,
            'status': TaskState.QUEUED.value,
            'payload': task_spec.payload,
            'max_retries': max_retries,
            'orchestra_id': task_spec.orchestra_id,
            'orchestra_phase': task_spec.orchestra_phase,
            'min_confidence': task_spec.min_confidence,
            'preferred_model': task_spec.preferred_model.value if task_spec.preferred_model else None,
            'timeout_seconds': task_spec.timeout_seconds,
            'cost_limit_usd': float(task_spec.cost_limit_usd) if task_spec.cost_limit_usd else None,
            'created_by': task_spec.created_by,
            'tags': task_spec.tags,
            'metadata': {}
        }
        
        # Insert into database
        self.db.insert_task(task_data)
        
        # Update orchestra stats if part of an orchestra
        if task_spec.orchestra_id:
            self.db.update_orchestra_stats(task_spec.orchestra_id)
        
        logger.info(f"Submitted task {task_id} (type={task_spec.type.value}, priority={task_spec.priority.value})")
        
        return task_id
    
    def get_task_status(self, task_id: str) -> Optional[TaskStatus]:
        """Get current status of a specific task.
        
        Args:
            task_id: Unique task identifier
            
        Returns:
            TaskStatus object or None if task not found
        """
        task_data = self.db.get_task(task_id)
        if not task_data:
            return None
        
        return TaskStatus(
            task_id=task_data['id'],
            task_type=TaskType(task_data['type']),
            state=TaskState(task_data['status']),
            priority=Priority(task_data['priority']),
            created_at=datetime.fromisoformat(task_data['created_at']),
            started_at=datetime.fromisoformat(task_data['started_at']) if task_data['started_at'] else None,
            completed_at=datetime.fromisoformat(task_data['completed_at']) if task_data['completed_at'] else None,
            next_retry_at=datetime.fromisoformat(task_data['next_retry_at']) if task_data['next_retry_at'] else None,
            retry_count=task_data['retry_count'],
            max_retries=task_data['max_retries'],
            orchestra_id=task_data['orchestra_id'],
            orchestra_phase=task_data['orchestra_phase'],
            tokens_consumed=0,  # TODO: Aggregate from task_runs
            cost_usd=Decimal(str(task_data.get('cost_limit_usd') or 0)),
            execution_time_seconds=0.0  # TODO: Calculate from timing
        )
    
    def list_tasks(self, filters: Optional[TaskFilters] = None) -> List[TaskSummary]:
        """List tasks with optional filtering.
        
        Args:
            filters: Optional filters for state, type, orchestra, etc.
            
        Returns:
            List of TaskSummary objects
        """
        if filters is None:
            filters = TaskFilters()
        
        # Convert enums to strings for database query
        states = [s.value for s in filters.states] if filters.states else None
        types = [t.value for t in filters.types] if filters.types else None
        
        tasks_data = self.db.list_tasks(
            states=states,
            types=types,
            orchestra_id=filters.orchestra_id,
            limit=filters.limit,
            offset=filters.offset
        )
        
        summaries = []
        for task_data in tasks_data:
            summary = TaskSummary(
                task_id=task_data['id'],
                task_type=TaskType(task_data['type']),
                state=TaskState(task_data['status']),
                priority=Priority(task_data['priority']),
                created_at=datetime.fromisoformat(task_data['created_at']),
                retry_count=task_data['retry_count'],
                orchestra_id=task_data['orchestra_id'],
                tags=task_data.get('tags', [])
            )
            
            # Extract title from payload if available
            payload = task_data.get('payload', {})
            if isinstance(payload, dict):
                summary.title = payload.get('title') or payload.get('name')
                summary.description = payload.get('description')
            
            summaries.append(summary)
        
        return summaries
    
    def cancel_task(self, task_id: str) -> bool:
        """Cancel a queued or running task.
        
        Args:
            task_id: Unique task identifier
            
        Returns:
            bool: True if task was cancelled, False if not found or not cancellable
        """
        success = self.db.cancel_task(task_id)
        
        if success:
            # Update orchestra stats if part of an orchestra
            task_data = self.db.get_task(task_id)
            if task_data and task_data['orchestra_id']:
                self.db.update_orchestra_stats(task_data['orchestra_id'])
            
            logger.info(f"Cancelled task {task_id}")
        
        return success
    
    def retry_failed_task(self, task_id: str) -> bool:
        """Manually retry a failed task.
        
        Args:
            task_id: Unique task identifier
            
        Returns:
            bool: True if task was queued for retry, False otherwise
        """
        task_data = self.db.get_task(task_id)
        if not task_data:
            logger.warning(f"Task {task_id} not found for retry")
            return False
        
        if task_data['status'] not in ['failed', 'permanently_failed']:
            logger.warning(f"Task {task_id} is not in failed state (current: {task_data['status']})")
            return False
        
        if task_data['retry_count'] >= task_data['max_retries']:
            logger.warning(f"Task {task_id} has exceeded max retries ({task_data['max_retries']})")
            return False
        
        # Reset task for retry
        success = self.db.update_task_status(
            task_id,
            TaskState.QUEUED.value,
            started_at=None,
            completed_at=None,
            next_retry_at=None
        )
        
        if success:
            logger.info(f"Queued task {task_id} for manual retry")
        
        return success
    
    def get_next_task(self, worker_id: str) -> Optional[Dict[str, Any]]:
        """Get the next available task for a worker.
        
        Args:
            worker_id: Unique worker identifier
            
        Returns:
            Dict with task data or None if no tasks available
        """
        # Update worker heartbeat
        self._worker_heartbeats[worker_id] = datetime.now()
        
        # Get next task from database
        task_data = self.db.get_next_task(worker_id)
        
        if task_data:
            logger.info(f"Assigned task {task_data['id']} to worker {worker_id}")
        
        return task_data
    
    def complete_task(
        self,
        task_id: str,
        result: TaskResult,
        worker_id: Optional[str] = None
    ) -> bool:
        """Mark a task as completed with results.
        
        Args:
            task_id: Unique task identifier
            result: Task execution result
            worker_id: Worker that completed the task
            
        Returns:
            bool: True if task was marked as completed
        """
        # Determine final state based on result
        final_state = result.state
        
        # Check if confidence meets minimum requirement
        task_data = self.db.get_task(task_id)
        if not task_data:
            logger.error(f"Task {task_id} not found for completion")
            return False
        
        min_confidence = task_data.get('min_confidence', 0.7)
        if result.confidence < min_confidence and final_state == TaskState.SUCCESS:
            logger.warning(f"Task {task_id} confidence {result.confidence} below minimum {min_confidence}")
            final_state = TaskState.FAILED
            result.state = final_state
            result.errors.append({
                'code': 'LOW_CONFIDENCE',
                'message': f'Result confidence {result.confidence} below required {min_confidence}',
                'severity': 'error'
            })
        
        # Update task status
        success = self.db.update_task_status(
            task_id,
            final_state.value,
            completed_at=datetime.now(),
            metadata={
                'result': result.dict(),
                'worker_id': worker_id,
                'completion_timestamp': datetime.now().isoformat()
            }
        )
        
        # Handle retry logic for failed tasks
        if final_state == TaskState.FAILED:
            self._handle_task_failure(task_id, result, task_data)
        
        # Update orchestra stats if part of an orchestra
        if task_data.get('orchestra_id'):
            self.db.update_orchestra_stats(task_data['orchestra_id'])
        
        logger.info(f"Completed task {task_id} with state {final_state.value}")
        
        return success
    
    def fail_task(
        self,
        task_id: str,
        error_message: str,
        error_type: str = 'permanent',
        worker_id: Optional[str] = None
    ) -> bool:
        """Mark a task as failed.
        
        Args:
            task_id: Unique task identifier
            error_message: Human-readable error description
            error_type: Type of error ('transient', 'permanent', 'quality')
            worker_id: Worker that was processing the task
            
        Returns:
            bool: True if task was marked as failed
        """
        task_data = self.db.get_task(task_id)
        if not task_data:
            logger.error(f"Task {task_id} not found for failure")
            return False
        
        # Create task run record
        run_id = str(uuid4())
        attempt_number = task_data['retry_count'] + 1
        
        self.db.insert_task_run({
            'id': run_id,
            'task_id': task_id,
            'attempt_number': attempt_number,
            'model': 'unknown',  # TODO: Track model being used
            'worker_id': worker_id,
            'status': TaskState.FAILED.value,
            'error_message': error_message,
            'error_type': error_type
        })
        
        # Update completion timestamp
        self.db.update_task_run(
            run_id,
            completed_at=datetime.now(),
            status=TaskState.FAILED.value
        )
        
        # Handle retry logic
        if error_type == 'transient' and task_data['retry_count'] < task_data['max_retries']:
            self._schedule_retry(task_id, task_data)
        else:
            # Mark as permanently failed and move to dead letter queue
            success = self.db.update_task_status(
                task_id,
                TaskState.PERMANENTLY_FAILED.value,
                completed_at=datetime.now()
            )
            
            if success:
                self.db.move_to_dead_letter(task_id, error_message)
        
        # Update orchestra stats if part of an orchestra
        if task_data.get('orchestra_id'):
            self.db.update_orchestra_stats(task_data['orchestra_id'])
        
        logger.info(f"Failed task {task_id}: {error_message}")
        
        return True
    
    def get_queue_stats(self) -> QueueStats:
        """Get comprehensive queue statistics.
        
        Returns:
            QueueStats object with current queue metrics
        """
        stats_data = self.db.get_queue_stats()
        
        # Calculate additional metrics
        active_workers = len([
            worker_id for worker_id, last_seen in self._worker_heartbeats.items()
            if datetime.now() - last_seen < timedelta(minutes=5)
        ])
        
        # Check for warnings
        queue_depth_warning = stats_data['queued'] > 50
        stale_tasks_warning = False  # TODO: Check for tasks stuck in running state
        
        return QueueStats(
            timestamp=stats_data['timestamp'],
            queued=stats_data['queued'],
            running=stats_data['running'],
            completed=stats_data['completed'],
            failed=stats_data['failed'],
            retrying=stats_data['retrying'],
            cancelled=stats_data['cancelled'],
            priority_breakdown=stats_data['priority_breakdown'],
            type_breakdown=stats_data['type_breakdown'],
            avg_execution_time_seconds=stats_data['avg_execution_time_seconds'],
            dead_letter_count=stats_data['dead_letter_count'],
            active_workers=active_workers,
            max_workers=stats_data['max_workers'],
            queue_depth_warning=queue_depth_warning,
            stale_tasks_warning=stale_tasks_warning,
            total_cost_today_usd=Decimal('0.00'),  # TODO: Calculate from task_runs
            total_tokens_consumed=0  # TODO: Sum from task_runs
        )
    
    def cleanup_stale_workers(self) -> int:
        """Clean up workers that haven't sent heartbeat recently.
        
        Returns:
            int: Number of workers cleaned up
        """
        cutoff = datetime.now() - timedelta(minutes=5)
        stale_workers = [
            worker_id for worker_id, last_seen in self._worker_heartbeats.items()
            if last_seen < cutoff
        ]
        
        for worker_id in stale_workers:
            del self._worker_heartbeats[worker_id]
            logger.info(f"Cleaned up stale worker {worker_id}")
        
        return len(stale_workers)
    
    def get_dead_letter_tasks(self, limit: int = 100) -> List[DeadLetterTask]:
        """Get tasks from dead letter queue.
        
        Args:
            limit: Maximum number of tasks to return
            
        Returns:
            List of DeadLetterTask objects
        """
        conn = self.db.get_connection()
        cursor = conn.execute("""
            SELECT * FROM dead_letter_queue 
            ORDER BY created_at DESC 
            LIMIT ?
        """, (limit,))
        
        tasks = []
        for row in cursor.fetchall():
            task_data = self.db._row_to_dict(row)
            tasks.append(DeadLetterTask(
                id=task_data['id'],
                original_task_id=task_data['original_task_id'],
                task_type=TaskType(task_data['task_type']),
                failure_reason=task_data['failure_reason'],
                failure_count=task_data['failure_count'],
                payload=task_data['payload'],
                created_at=datetime.fromisoformat(task_data['created_at']),
                error_patterns=task_data.get('error_patterns', []),
                suggested_fixes=task_data.get('suggested_fixes', [])
            ))
        
        return tasks
    
    # Private Methods
    
    def _handle_task_failure(
        self,
        task_id: str,
        result: TaskResult,
        task_data: Dict[str, Any]
    ) -> None:
        """Handle failed task with retry logic.
        
        Args:
            task_id: Task ID that failed
            result: Task result with failure info
            task_data: Current task data from database
        """
        current_retry_count = task_data['retry_count']
        max_retries = task_data['max_retries']
        
        # Determine if this is a retryable failure
        retryable = any(
            error.get('severity') in ['warning', 'error'] 
            for error in result.errors
        )
        
        if retryable and current_retry_count < max_retries:
            self._schedule_retry(task_id, task_data)
        else:
            # Permanently failed
            self.db.update_task_status(
                task_id,
                TaskState.PERMANENTLY_FAILED.value,
                completed_at=datetime.now()
            )
            
            # Move to dead letter queue
            failure_reason = '; '.join([
                error.get('message', 'Unknown error') 
                for error in result.errors
            ]) or 'Task failed with low confidence'
            
            self.db.move_to_dead_letter(task_id, failure_reason)
    
    def _schedule_retry(self, task_id: str, task_data: Dict[str, Any]) -> None:
        """Schedule a task for retry with exponential backoff.
        
        Args:
            task_id: Task ID to retry
            task_data: Current task data
        """
        retry_count = task_data['retry_count'] + 1
        delay_seconds = calculate_retry_delay(retry_count)
        next_retry_at = datetime.now() + timedelta(seconds=delay_seconds)
        
        self.db.update_task_status(
            task_id,
            TaskState.RETRY.value,
            retry_count=retry_count,
            next_retry_at=next_retry_at,
            started_at=None  # Reset started time
        )
        
        logger.info(f"Scheduled task {task_id} for retry #{retry_count} in {delay_seconds}s")
    
    def close(self) -> None:
        """Close database connections and cleanup resources."""
        self.db.close()