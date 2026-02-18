"""Progress Streaming System for the Orchestration Engine.

Provides real-time progress updates stored in SQLite with streaming capabilities.
Tracks task lifecycle events with timestamps and contextual information.
"""

import json
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Any, List, Optional, Iterator
from uuid import uuid4

from pydantic import BaseModel, Field

from .db import Database
from .schemas import TaskState


logger = logging.getLogger(__name__)


class ProgressEventType(str, Enum):
    """Types of progress events."""
    QUEUED = "queued"                    # Task added to queue
    STARTED = "started"                  # Task execution begun  
    PROGRESS_UPDATE = "progress_update"  # Intermediate progress
    MODEL_SELECTED = "model_selected"    # Model tier chosen
    SESSION_CREATED = "session_created"  # OpenClaw session started
    SESSION_ENDED = "session_ended"      # OpenClaw session ended
    RETRY_SCHEDULED = "retry_scheduled"  # Retry queued due to failure
    ESCALATED = "escalated"              # Model tier escalated
    COMPLETED = "completed"              # Task successfully completed
    FAILED = "failed"                    # Task failed permanently
    CANCELLED = "cancelled"              # Task cancelled by user
    TIMEOUT = "timeout"                  # Task timed out
    RESOURCE_LIMIT = "resource_limit"    # Resource limit reached
    CIRCUIT_BREAKER = "circuit_breaker"  # Circuit breaker triggered


class ProgressEvent(BaseModel):
    """Individual progress event."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    event_type: ProgressEventType
    timestamp: datetime = Field(default_factory=datetime.now)
    
    # Event-specific data
    message: Optional[str] = None
    progress_percentage: Optional[float] = Field(None, ge=0.0, le=100.0)
    details: Dict[str, Any] = Field(default_factory=dict)
    
    # Context information
    worker_id: Optional[str] = None
    session_id: Optional[str] = None
    model_tier: Optional[str] = None
    attempt_number: int = 1
    
    # Resource metrics
    tokens_used: Optional[int] = None
    cost_usd: Optional[str] = None  # Decimal as string for JSON serialization
    memory_mb: Optional[int] = None
    
    class Config:
        """Pydantic config for JSON serialization."""
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class TaskProgress(BaseModel):
    """Aggregate progress information for a task."""
    task_id: str
    current_state: TaskState
    progress_percentage: float = 0.0
    
    # Timing information
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    last_update: datetime = Field(default_factory=datetime.now)
    
    # Current status
    current_message: Optional[str] = None
    current_model: Optional[str] = None
    attempt_number: int = 1
    
    # Aggregate metrics
    total_tokens: int = 0
    total_cost_usd: str = "0.00"  # Decimal as string
    peak_memory_mb: int = 0
    
    # Event counts
    retry_count: int = 0
    escalation_count: int = 0
    
    # Execution timeline
    events: List[ProgressEvent] = Field(default_factory=list)
    
    @property
    def execution_time_seconds(self) -> Optional[float]:
        """Calculate current execution time."""
        if self.started_at is None:
            return None
        
        end_time = self.completed_at or datetime.now()
        return (end_time - self.started_at).total_seconds()
    
    @property
    def is_active(self) -> bool:
        """Check if task is currently active (running or retrying)."""
        return self.current_state in [TaskState.RUNNING, TaskState.RETRY]
    
    @property
    def is_terminal(self) -> bool:
        """Check if task is in a terminal state."""
        return self.current_state in [
            TaskState.SUCCESS, 
            TaskState.FAILED, 
            TaskState.PERMANENTLY_FAILED,
            TaskState.CANCELLED
        ]


class ProgressTracker:
    """Real-time progress tracking and streaming system."""
    
    def __init__(self, database: Database):
        """Initialize the progress tracker.
        
        Args:
            database: Database instance for persistence
        """
        self.db = database
        self._init_tables()
    
    def _init_tables(self) -> None:
        """Initialize progress tracking tables."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS progress_events (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                message TEXT,
                progress_percentage REAL,
                details TEXT,  -- JSON
                worker_id TEXT,
                session_id TEXT,
                model_tier TEXT,
                attempt_number INTEGER DEFAULT 1,
                tokens_used INTEGER,
                cost_usd TEXT,
                memory_mb INTEGER,
                FOREIGN KEY(task_id) REFERENCES tasks(id),
                INDEX idx_progress_task_time (task_id, timestamp),
                INDEX idx_progress_type (event_type, timestamp)
            )
        """)
        
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS task_progress_summary (
                task_id TEXT PRIMARY KEY,
                current_state TEXT NOT NULL,
                progress_percentage REAL DEFAULT 0.0,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                last_update TIMESTAMP NOT NULL,
                current_message TEXT,
                current_model TEXT,
                attempt_number INTEGER DEFAULT 1,
                total_tokens INTEGER DEFAULT 0,
                total_cost_usd TEXT DEFAULT '0.00',
                peak_memory_mb INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                escalation_count INTEGER DEFAULT 0,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )
        """)
    
    def record_event(self, event: ProgressEvent) -> None:
        """Record a progress event.
        
        Args:
            event: Progress event to record
        """
        try:
            # Insert event record
            self.db.execute("""
                INSERT INTO progress_events (
                    id, task_id, event_type, timestamp, message, progress_percentage,
                    details, worker_id, session_id, model_tier, attempt_number,
                    tokens_used, cost_usd, memory_mb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                event.id, event.task_id, event.event_type.value, event.timestamp,
                event.message, event.progress_percentage, json.dumps(event.details),
                event.worker_id, event.session_id, event.model_tier, event.attempt_number,
                event.tokens_used, event.cost_usd, event.memory_mb
            ])
            
            # Update task progress summary
            self._update_task_summary(event)
            
            logger.debug(f"Recorded progress event {event.event_type} for task {event.task_id}")
            
        except Exception as e:
            logger.error(f"Failed to record progress event: {e}")
    
    def _update_task_summary(self, event: ProgressEvent) -> None:
        """Update the task progress summary based on new event."""
        # Map event types to task states
        state_mapping = {
            ProgressEventType.QUEUED: TaskState.QUEUED,
            ProgressEventType.STARTED: TaskState.RUNNING,
            ProgressEventType.PROGRESS_UPDATE: TaskState.RUNNING,
            ProgressEventType.MODEL_SELECTED: TaskState.RUNNING,
            ProgressEventType.SESSION_CREATED: TaskState.RUNNING,
            ProgressEventType.SESSION_ENDED: TaskState.RUNNING,
            ProgressEventType.RETRY_SCHEDULED: TaskState.RETRY,
            ProgressEventType.ESCALATED: TaskState.RETRY,
            ProgressEventType.COMPLETED: TaskState.SUCCESS,
            ProgressEventType.FAILED: TaskState.FAILED,
            ProgressEventType.CANCELLED: TaskState.CANCELLED,
            ProgressEventType.TIMEOUT: TaskState.FAILED,
            ProgressEventType.RESOURCE_LIMIT: TaskState.FAILED,
            ProgressEventType.CIRCUIT_BREAKER: TaskState.PERMANENTLY_FAILED,
        }
        
        new_state = state_mapping.get(event.event_type, TaskState.QUEUED)
        
        # Upsert progress summary
        self.db.execute("""
            INSERT INTO task_progress_summary (
                task_id, current_state, progress_percentage, created_at, 
                last_update, current_message, current_model, attempt_number,
                total_tokens, total_cost_usd, peak_memory_mb, retry_count, escalation_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                current_state = EXCLUDED.current_state,
                progress_percentage = COALESCE(EXCLUDED.progress_percentage, progress_percentage),
                last_update = EXCLUDED.last_update,
                current_message = COALESCE(EXCLUDED.current_message, current_message),
                current_model = COALESCE(EXCLUDED.current_model, current_model),
                attempt_number = EXCLUDED.attempt_number,
                total_tokens = total_tokens + COALESCE(EXCLUDED.total_tokens, 0),
                total_cost_usd = EXCLUDED.total_cost_usd,
                peak_memory_mb = MAX(peak_memory_mb, COALESCE(EXCLUDED.peak_memory_mb, 0)),
                retry_count = retry_count + CASE WHEN EXCLUDED.current_state = 'retry' THEN 1 ELSE 0 END,
                escalation_count = escalation_count + CASE WHEN ? = 'escalated' THEN 1 ELSE 0 END,
                started_at = CASE WHEN started_at IS NULL AND EXCLUDED.current_state = 'running' 
                            THEN EXCLUDED.last_update ELSE started_at END,
                completed_at = CASE WHEN EXCLUDED.current_state IN ('success', 'failed', 'permanently_failed', 'cancelled')
                              THEN EXCLUDED.last_update ELSE completed_at END
        """, [
            event.task_id, new_state.value, event.progress_percentage, event.timestamp,
            event.timestamp, event.message, event.model_tier, event.attempt_number,
            event.tokens_used or 0, event.cost_usd or "0.00", event.memory_mb or 0, 
            0, 0,  # retry_count and escalation_count are calculated in the UPDATE
            event.event_type.value  # for escalation count check
        ])
    
    def get_task_progress(self, task_id: str, include_events: bool = True) -> Optional[TaskProgress]:
        """Get current progress for a task.
        
        Args:
            task_id: Task identifier
            include_events: Whether to include full event history
            
        Returns:
            TaskProgress object or None if task not found
        """
        # Get summary
        summary = self.db.fetch_one("""
            SELECT * FROM task_progress_summary WHERE task_id = ?
        """, [task_id])
        
        if not summary:
            return None
        
        # Build TaskProgress object
        progress = TaskProgress(
            task_id=summary['task_id'],
            current_state=TaskState(summary['current_state']),
            progress_percentage=summary['progress_percentage'] or 0.0,
            created_at=summary['created_at'],
            started_at=summary['started_at'],
            completed_at=summary['completed_at'],
            last_update=summary['last_update'],
            current_message=summary['current_message'],
            current_model=summary['current_model'],
            attempt_number=summary['attempt_number'],
            total_tokens=summary['total_tokens'],
            total_cost_usd=summary['total_cost_usd'],
            peak_memory_mb=summary['peak_memory_mb'],
            retry_count=summary['retry_count'],
            escalation_count=summary['escalation_count']
        )
        
        # Add events if requested
        if include_events:
            events = self.db.fetch_all("""
                SELECT * FROM progress_events 
                WHERE task_id = ? 
                ORDER BY timestamp ASC
            """, [task_id])
            
            progress.events = [
                ProgressEvent(
                    id=event['id'],
                    task_id=event['task_id'],
                    event_type=ProgressEventType(event['event_type']),
                    timestamp=event['timestamp'],
                    message=event['message'],
                    progress_percentage=event['progress_percentage'],
                    details=json.loads(event['details'] or '{}'),
                    worker_id=event['worker_id'],
                    session_id=event['session_id'],
                    model_tier=event['model_tier'],
                    attempt_number=event['attempt_number'],
                    tokens_used=event['tokens_used'],
                    cost_usd=event['cost_usd'],
                    memory_mb=event['memory_mb']
                )
                for event in events
            ]
        
        return progress
    
    def get_active_tasks(self) -> List[str]:
        """Get list of currently active task IDs."""
        results = self.db.fetch_all("""
            SELECT task_id FROM task_progress_summary 
            WHERE current_state IN ('running', 'retry')
            ORDER BY last_update DESC
        """)
        
        return [row['task_id'] for row in results]
    
    def stream_task_events(self, task_id: str, since: Optional[datetime] = None) -> Iterator[ProgressEvent]:
        """Stream progress events for a task.
        
        Args:
            task_id: Task identifier
            since: Only return events after this timestamp
            
        Yields:
            ProgressEvent objects in chronological order
        """
        query = "SELECT * FROM progress_events WHERE task_id = ?"
        params = [task_id]
        
        if since:
            query += " AND timestamp > ?"
            params.append(since)
        
        query += " ORDER BY timestamp ASC"
        
        events = self.db.fetch_all(query, params)
        
        for event_row in events:
            yield ProgressEvent(
                id=event_row['id'],
                task_id=event_row['task_id'],
                event_type=ProgressEventType(event_row['event_type']),
                timestamp=event_row['timestamp'],
                message=event_row['message'],
                progress_percentage=event_row['progress_percentage'],
                details=json.loads(event_row['details'] or '{}'),
                worker_id=event_row['worker_id'],
                session_id=event_row['session_id'],
                model_tier=event_row['model_tier'],
                attempt_number=event_row['attempt_number'],
                tokens_used=event_row['tokens_used'],
                cost_usd=event_row['cost_usd'],
                memory_mb=event_row['memory_mb']
            )
    
    def cleanup_old_events(self, older_than_days: int = 7) -> int:
        """Clean up old progress events to prevent database bloat.
        
        Args:
            older_than_days: Remove events older than this many days
            
        Returns:
            Number of events removed
        """
        cutoff = datetime.now() - timedelta(days=older_than_days)
        
        result = self.db.execute("""
            DELETE FROM progress_events 
            WHERE timestamp < ? 
            AND task_id NOT IN (
                SELECT task_id FROM task_progress_summary 
                WHERE current_state IN ('running', 'retry')
            )
        """, [cutoff])
        
        deleted_count = result.rowcount if hasattr(result, 'rowcount') else 0
        logger.info(f"Cleaned up {deleted_count} old progress events")
        
        return deleted_count
    
    # Convenience methods for common events
    
    def task_queued(self, task_id: str, message: str = None) -> None:
        """Record task queued event."""
        self.record_event(ProgressEvent(
            task_id=task_id,
            event_type=ProgressEventType.QUEUED,
            message=message or "Task queued for execution"
        ))
    
    def task_started(self, task_id: str, worker_id: str, session_id: str = None, message: str = None) -> None:
        """Record task started event."""
        self.record_event(ProgressEvent(
            task_id=task_id,
            event_type=ProgressEventType.STARTED,
            message=message or "Task execution started",
            worker_id=worker_id,
            session_id=session_id
        ))
    
    def task_progress(self, task_id: str, percentage: float, message: str, 
                     worker_id: str = None, details: Dict[str, Any] = None) -> None:
        """Record task progress update."""
        self.record_event(ProgressEvent(
            task_id=task_id,
            event_type=ProgressEventType.PROGRESS_UPDATE,
            message=message,
            progress_percentage=percentage,
            worker_id=worker_id,
            details=details or {}
        ))
    
    def task_completed(self, task_id: str, worker_id: str = None, 
                      tokens_used: int = None, cost_usd: str = None) -> None:
        """Record task completion."""
        self.record_event(ProgressEvent(
            task_id=task_id,
            event_type=ProgressEventType.COMPLETED,
            message="Task completed successfully",
            progress_percentage=100.0,
            worker_id=worker_id,
            tokens_used=tokens_used,
            cost_usd=cost_usd
        ))
    
    def task_failed(self, task_id: str, error_message: str, worker_id: str = None,
                   attempt_number: int = 1, is_permanent: bool = False) -> None:
        """Record task failure."""
        event_type = ProgressEventType.CIRCUIT_BREAKER if is_permanent else ProgressEventType.FAILED
        
        self.record_event(ProgressEvent(
            task_id=task_id,
            event_type=event_type,
            message=f"Task failed: {error_message}",
            worker_id=worker_id,
            attempt_number=attempt_number
        ))
    
    def task_retry_scheduled(self, task_id: str, retry_at: datetime, attempt_number: int,
                           reason: str, worker_id: str = None) -> None:
        """Record retry scheduling."""
        self.record_event(ProgressEvent(
            task_id=task_id,
            event_type=ProgressEventType.RETRY_SCHEDULED,
            message=f"Retry {attempt_number} scheduled: {reason}",
            worker_id=worker_id,
            attempt_number=attempt_number,
            details={"retry_at": retry_at.isoformat(), "reason": reason}
        ))
    
    def model_escalated(self, task_id: str, from_tier: str, to_tier: str, 
                       attempt_number: int, worker_id: str = None) -> None:
        """Record model tier escalation."""
        self.record_event(ProgressEvent(
            task_id=task_id,
            event_type=ProgressEventType.ESCALATED,
            message=f"Model escalated from {from_tier} to {to_tier}",
            model_tier=to_tier,
            attempt_number=attempt_number,
            worker_id=worker_id,
            details={"from_tier": from_tier, "to_tier": to_tier}
        ))