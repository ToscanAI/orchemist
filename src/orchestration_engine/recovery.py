"""Error Recovery and Retry Logic for the Orchestration Engine.

Implements intelligent error classification, exponential backoff, model tier escalation,
and circuit breaker patterns based on the error recovery documentation.
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

from .db import Database
from .config import EngineConfig
from .errors import (
    AuthenticationError,
    GatewayHTTPError,
    GatewayUnavailableError,
    RateLimitError,
    SpawnNoPromptDelivered,
    SpawnTransportTimeout,
)
from .schemas import TaskType, ModelTier, TaskState, select_model_tier
from .timestamps import now_utc


logger = logging.getLogger(__name__)


@dataclass
class ExecutorRetryConfig:
    """Lightweight retry configuration for OpenClawExecutor — no DB or config dependency.

    This dataclass is intentionally free of Database / EngineConfig dependencies so
    that OpenClawExecutor can use it without the full engine machinery.
    """

    max_attempts: int = 3
    """Total attempts (1 original + max_attempts-1 retries)."""

    backoff_base: float = 2.0
    """Base wait in seconds on first retry."""

    backoff_multiplier: float = 4.0
    """Exponent base for backoff: wait = backoff_base * backoff_multiplier^attempt_index."""

    backoff_max: float = 60.0
    """Cap on computed backoff (seconds)."""

    circuit_breaker_threshold: int = 5
    """Consecutive failures required to open the circuit breaker."""

    circuit_breaker_reset_seconds: int = 300
    """Seconds before an open circuit breaker transitions to half-open (5 minutes)."""

    socket_timeout_initial: float = 30.0
    """Initial HTTP socket timeout (seconds) for the gateway spawn call on the
    first attempt. Preserves the historical hardcoded 30s (issue #732)."""

    socket_timeout_multiplier: float = 2.0
    """Per-retry multiplier applied to the spawn socket timeout (30→60→120)."""

    socket_timeout_max: float = 120.0
    """Cap on the per-retry spawn socket timeout (seconds)."""

    spawn_startup_grace_seconds: float = 60.0
    """Grace window (seconds) after a successful spawn within which the session
    must produce its first message; otherwise the task fails fast with
    ``spawn_no_prompt_delivered`` rather than polling until the full task
    timeout (issue #732, Bug B)."""


def classify_exception_error_type(exc: Exception) -> "ErrorType":
    """Map an exception instance to an :class:`ErrorType` for retry decisions.

    Mapping rules (ordered, first match wins):

    * :class:`~.errors.RateLimitError` (HTTP 429)             → ``RATE_LIMIT``
    * :class:`~.errors.AuthenticationError` (401/403)         → ``PERMANENT``
    * :class:`~.errors.GatewayUnavailableError` (502/503/504) → ``TRANSIENT``
    * Other :class:`~.errors.GatewayHTTPError` with 4xx       → ``PERMANENT``
    * Other :class:`~.errors.GatewayHTTPError` (5xx etc.)     → ``TRANSIENT``
    * :class:`TimeoutError`                                    → ``TIMEOUT``
    * :class:`RuntimeError` with ``'timeout'`` in message     → ``TIMEOUT``
    * :class:`RuntimeError` with ``'garbage-collected'``       → ``PERMANENT``
    * Any other :class:`Exception`                            → ``TRANSIENT``

    Args:
        exc: The exception to classify.

    Returns:
        The :class:`ErrorType` that best describes the error for retry purposes.
    """
    # RateLimitError is a GatewayHTTPError subclass — check it first.
    if isinstance(exc, RateLimitError):
        return ErrorType.RATE_LIMIT

    # AuthenticationError is a GatewayHTTPError subclass — check before generic HTTPError.
    if isinstance(exc, AuthenticationError):
        return ErrorType.PERMANENT

    # GatewayUnavailableError (502/503/504) is retryable.
    if isinstance(exc, GatewayUnavailableError):
        return ErrorType.TRANSIENT

    # Any remaining 4xx GatewayHTTPError is a client/permanent error.
    if isinstance(exc, GatewayHTTPError):
        if 400 <= exc.status_code < 500:
            return ErrorType.PERMANENT
        # 5xx and other codes — treat as transient.
        return ErrorType.TRANSIENT

    # Transport-layer timeout during a gateway spawn HTTP call (issue #732).
    # SpawnTransportTimeout subclasses TimeoutError, so this MUST be checked
    # BEFORE the generic TimeoutError branch below (first-match-wins), else it
    # would be swallowed as a task-deadline TIMEOUT and incorrectly increment
    # the circuit breaker / escalate the model.
    if isinstance(exc, SpawnTransportTimeout):
        return ErrorType.TRANSPORT_TIMEOUT

    # A spawned session that never delivered a first message within the startup
    # grace window is a gateway prompt-delivery symptom — same non-CB-incrementing,
    # non-escalating treatment as a transport timeout (issue #732, Bug B). Its
    # distinct error code is set separately in the executor's error-code branch.
    if isinstance(exc, SpawnNoPromptDelivered):
        return ErrorType.TRANSPORT_TIMEOUT

    # Python's built-in TimeoutError (e.g. the _run_session task-deadline).
    if isinstance(exc, TimeoutError):
        return ErrorType.TIMEOUT

    # RuntimeError variants raised by _run_session.
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "garbage-collected" in msg or "garbage_collected" in msg:
            # Session was evicted by the gateway — no point retrying the same key.
            return ErrorType.PERMANENT
        if "timeout" in msg:
            return ErrorType.TIMEOUT

    # Default: assume transient (network glitch, temporary API hiccup, etc.)
    return ErrorType.TRANSIENT


class ErrorType(str, Enum):
    """Error classification types."""
    TRANSIENT = "transient"      # Temporary issues, should retry
    PERMANENT = "permanent"      # Permanent failures, no retry
    QUALITY = "quality"         # Quality issues, retry with better model
    RESOURCE = "resource"       # Resource exhaustion, wait and retry
    TIMEOUT = "timeout"         # Task timeout, may retry with longer limit
    RATE_LIMIT = "rate_limit"   # API rate limiting, backoff and retry
    TRANSPORT_TIMEOUT = "transport_timeout"  # HTTP socket timeout during spawn — do NOT open CB / escalate (#732)


class ErrorSeverity(str, Enum):
    """Error severity levels."""
    LOW = "low"           # Minor issues, continue processing
    MEDIUM = "medium"     # Moderate issues, may affect quality
    HIGH = "high"         # Serious issues, likely to cause failure
    CRITICAL = "critical" # Critical issues, immediate failure


@dataclass
class ErrorPattern:
    """Pattern for error classification."""
    keywords: List[str]           # Keywords to match in error messages
    error_type: ErrorType         # Type of error this pattern represents
    severity: ErrorSeverity       # Severity level
    max_retries: int = 3          # Maximum retry attempts
    backoff_multiplier: float = 2.0  # Exponential backoff multiplier
    escalate_model: bool = False  # Whether to escalate model tier
    circuit_breaker: bool = False # Whether to trigger circuit breaker
    
    def matches(self, error_message: str) -> bool:
        """Check if error message matches this pattern."""
        error_lower = error_message.lower()
        return any(keyword.lower() in error_lower for keyword in self.keywords)


@dataclass
class CircuitBreakerState:
    """Circuit breaker state for a task type or model."""
    name: str                           # Circuit breaker identifier
    failure_count: int = 0              # Consecutive failures
    last_failure: Optional[datetime] = None  # Last failure timestamp
    opened_at: Optional[datetime] = None     # When circuit was opened
    state: str = "closed"               # closed, open, half_open
    
    def is_open(self, threshold: int, reset_timeout_minutes: int) -> bool:
        """Check if circuit breaker is open."""
        if self.state == "open" and self.opened_at:
            # Check if reset timeout has passed
            opened = self.opened_at if self.opened_at.tzinfo else self.opened_at.replace(tzinfo=timezone.utc)
            if now_utc() - opened > timedelta(minutes=reset_timeout_minutes):
                self.state = "half_open"
                return False
            return True
        
        return self.failure_count >= threshold
    
    def record_success(self) -> None:
        """Record successful execution."""
        self.failure_count = 0
        self.state = "closed"
        self.opened_at = None
    
    def record_failure(self, threshold: int) -> None:
        """Record failed execution."""
        self.failure_count += 1
        self.last_failure = now_utc()

        if self.failure_count >= threshold and self.state != "open":
            self.state = "open"
            self.opened_at = now_utc()


@dataclass
class RetryAttempt:
    """Information about a retry attempt."""
    attempt_number: int
    scheduled_at: datetime
    executed_at: Optional[datetime] = None
    model_tier: Optional[str] = None
    error_type: Optional[ErrorType] = None
    error_message: Optional[str] = None
    backoff_seconds: int = 0


@dataclass
class TaskRetryState:
    """Retry state for a specific task."""
    task_id: str
    task_type: TaskType
    original_model_tier: str
    max_retries: int
    attempts: List[RetryAttempt] = field(default_factory=list)
    escalation_path: List[str] = field(default_factory=list)
    
    @property
    def current_attempt(self) -> int:
        """Get current attempt number."""
        return len(self.attempts)
    
    @property
    def next_model_tier(self) -> str:
        """Get next model tier for escalation."""
        if not self.escalation_path:
            return self.original_model_tier
        
        escalation_index = min(self.current_attempt, len(self.escalation_path) - 1)
        return self.escalation_path[escalation_index]
    
    def should_retry(self) -> bool:
        """Check if task should be retried."""
        return self.current_attempt < self.max_retries
    
    def schedule_retry(self, error_type: ErrorType, error_message: str, 
                      backoff_base: int, backoff_max: int) -> datetime:
        """Schedule next retry attempt."""
        backoff_seconds = min(
            backoff_base * (2 ** self.current_attempt),
            backoff_max
        )
        
        scheduled_at = now_utc() + timedelta(seconds=backoff_seconds)
        
        attempt = RetryAttempt(
            attempt_number=self.current_attempt + 1,
            scheduled_at=scheduled_at,
            model_tier=self.next_model_tier,
            error_type=error_type,
            error_message=error_message,
            backoff_seconds=backoff_seconds
        )
        
        self.attempts.append(attempt)
        return scheduled_at


class ErrorClassifier:
    """Classifies errors based on patterns and context."""
    
    def __init__(self):
        """Initialize with predefined error patterns."""
        self.patterns = [
            # Transient errors - should retry
            ErrorPattern(
                keywords=["timeout", "connection reset", "connection refused", "network error", 
                         "temporary failure", "service unavailable", "try again"],
                error_type=ErrorType.TRANSIENT,
                severity=ErrorSeverity.MEDIUM,
                max_retries=3
            ),
            
            # Resource errors - wait and retry
            ErrorPattern(
                keywords=["out of memory", "resource exhausted", "quota exceeded", 
                         "too many requests", "capacity limit"],
                error_type=ErrorType.RESOURCE,
                severity=ErrorSeverity.HIGH,
                max_retries=5,
                backoff_multiplier=3.0
            ),
            
            # Rate limiting - backoff and retry
            ErrorPattern(
                keywords=["rate limit", "too many requests", "429", "throttle", "rate exceeded"],
                error_type=ErrorType.RATE_LIMIT,
                severity=ErrorSeverity.MEDIUM,
                max_retries=8,
                backoff_multiplier=2.5
            ),
            
            # Quality errors - retry with better model
            ErrorPattern(
                keywords=["confidence too low", "quality check failed", "validation failed",
                         "insufficient quality", "hallucination detected"],
                error_type=ErrorType.QUALITY,
                severity=ErrorSeverity.MEDIUM,
                max_retries=3,
                escalate_model=True
            ),
            
            # Timeout errors - may retry with longer timeout
            ErrorPattern(
                keywords=["task timeout", "execution timeout", "deadline exceeded"],
                error_type=ErrorType.TIMEOUT,
                severity=ErrorSeverity.MEDIUM,
                max_retries=2
            ),
            
            # Permanent errors - don't retry
            ErrorPattern(
                keywords=["invalid task", "malformed input", "authentication failed",
                         "permission denied", "not authorized", "invalid api key",
                         "model not available", "unsupported operation"],
                error_type=ErrorType.PERMANENT,
                severity=ErrorSeverity.CRITICAL,
                max_retries=0
            ),
            
            # Circuit breaker triggers
            ErrorPattern(
                keywords=["model failure", "repeated failures", "system overload"],
                error_type=ErrorType.TRANSIENT,
                severity=ErrorSeverity.CRITICAL,
                max_retries=1,
                circuit_breaker=True
            ),
        ]
    
    def classify(self, error_message: str, context: Dict[str, Any] = None) -> Tuple[ErrorType, ErrorSeverity]:
        """Classify an error based on message and context.
        
        Args:
            error_message: Error message to classify
            context: Additional context (model, task_type, etc.)
            
        Returns:
            Tuple of (error_type, severity)
        """
        error_lower = error_message.lower()
        
        # Check explicit patterns first
        for pattern in self.patterns:
            if pattern.matches(error_message):
                logger.debug(f"Error classified as {pattern.error_type} (severity: {pattern.severity})")
                return pattern.error_type, pattern.severity
        
        # Context-based classification
        if context:
            # If model is known to be unreliable, treat as quality issue
            if context.get('model_tier') == 'haiku' and 'failed' in error_lower:
                return ErrorType.QUALITY, ErrorSeverity.MEDIUM
            
            # If error mentions specific models
            if any(model in error_lower for model in ['haiku', 'sonnet', 'opus']):
                return ErrorType.TRANSIENT, ErrorSeverity.MEDIUM
        
        # Default classification for unknown errors
        logger.warning(f"Unknown error pattern, defaulting to transient: {error_message}")
        return ErrorType.TRANSIENT, ErrorSeverity.MEDIUM
    
    def get_retry_config(self, error_type: ErrorType, task_type: TaskType) -> Dict[str, Any]:
        """Get retry configuration for error type and task type.
        
        Args:
            error_type: Type of error
            task_type: Type of task
            
        Returns:
            Dictionary with retry configuration
        """
        base_config = {
            "max_retries": 3,
            "backoff_multiplier": 2.0,
            "escalate_model": False
        }
        
        # Error type specific adjustments
        if error_type == ErrorType.RATE_LIMIT:
            base_config.update({
                "max_retries": 8,
                "backoff_multiplier": 2.5
            })
        elif error_type == ErrorType.QUALITY:
            base_config.update({
                "max_retries": 3,
                "escalate_model": True
            })
        elif error_type == ErrorType.RESOURCE:
            base_config.update({
                "max_retries": 5,
                "backoff_multiplier": 3.0
            })
        elif error_type == ErrorType.PERMANENT:
            base_config.update({
                "max_retries": 0
            })
        
        # Task type specific adjustments
        task_configs = {
            TaskType.CONTENT: {"max_retries": 3},
            TaskType.CODE: {"max_retries": 2},
            TaskType.RESEARCH: {"max_retries": 3},
            TaskType.TRANSLATION: {"max_retries": 4},
            TaskType.REVIEW: {"max_retries": 2}
        }
        
        if task_type in task_configs:
            base_config.update(task_configs[task_type])
        
        return base_config


class RecoveryManager:
    """Manages error recovery, retries, and circuit breakers."""
    
    def __init__(self, database: Database, config: EngineConfig):
        """Initialize the recovery manager.
        
        Args:
            database: Database instance for persistence
            config: Engine configuration
        """
        self.db = database
        self.config = config
        self.classifier = ErrorClassifier()
        
        # Thread lock for mutable state
        self._lock = threading.Lock()
        
        # Circuit breaker states
        self._circuit_breakers: Dict[str, CircuitBreakerState] = {}
        
        # Task retry states (in-memory cache)
        self._retry_states: Dict[str, TaskRetryState] = {}
        
        self._init_tables()
    
    def _init_tables(self) -> None:
        """Initialize recovery tracking tables."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS retry_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                scheduled_at TIMESTAMP NOT NULL,
                executed_at TIMESTAMP,
                model_tier TEXT,
                error_type TEXT,
                error_message TEXT,
                backoff_seconds INTEGER,
                success BOOLEAN
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_retry_task ON retry_attempts(task_id)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_retry_schedule ON retry_attempts(scheduled_at)"
        )

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS circuit_breaker_state (
                name TEXT PRIMARY KEY,
                failure_count INTEGER DEFAULT 0,
                last_failure TIMESTAMP,
                opened_at TIMESTAMP,
                state TEXT DEFAULT 'closed'
            )
        """)

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS error_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_message TEXT NOT NULL,
                error_type TEXT NOT NULL,
                task_type TEXT,
                model_tier TEXT,
                frequency INTEGER DEFAULT 1,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(error_message, task_type, model_tier)
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_error_message ON error_patterns(error_message)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_error_type ON error_patterns(error_type)"
        )
    
    def handle_task_failure(self, task_id: str, task_type: TaskType, 
                          error_message: str, model_tier: str = None,
                          context: Dict[str, Any] = None) -> Tuple[bool, Optional[datetime], Optional[str]]:
        """Handle task failure and determine retry strategy.
        
        Args:
            task_id: Failed task ID
            task_type: Type of task
            error_message: Error message
            model_tier: Model tier that failed
            context: Additional context
            
        Returns:
            Tuple of (should_retry, retry_at, next_model_tier)
        """
        # Classify error
        error_type, severity = self.classifier.classify(error_message, context or {})
        
        # Record error pattern for learning
        self._record_error_pattern(error_message, error_type, task_type, model_tier)
        
        # Thread-safe access to retry states and circuit breakers
        with self._lock:
            # Determine whether this is the task's first failure (not a retry attempt).
            is_first_failure = task_id not in self._retry_states
            
            # Get or create retry state
            retry_state = self._get_retry_state(task_id, task_type, model_tier or "sonnet-4")
        
        # Check circuit breakers
        cb_key = f"{task_type.value}:{model_tier or 'default'}"
        if self._check_circuit_breaker(cb_key, error_type, increment=is_first_failure):
            logger.warning(f"Circuit breaker open for {cb_key}, not retrying task {task_id}")
            return False, None, None
        
        # Determine if we should retry
        if error_type == ErrorType.PERMANENT or not retry_state.should_retry():
            logger.info(f"Task {task_id} not retrying: error_type={error_type}, attempts={retry_state.current_attempt}")
            return False, None, None
        
        # Schedule retry
        retry_config = self.classifier.get_retry_config(error_type, task_type)
        retry_at = retry_state.schedule_retry(
            error_type, error_message,
            self.config.retry.backoff_base,
            self.config.retry.backoff_max
        )
        
        # Get next model tier
        next_model = retry_state.next_model_tier
        if retry_config.get('escalate_model') and self.config.models.escalation_enabled:
            # Use the escalation path from schemas
            next_model = select_model_tier(task_type, retry_state.current_attempt + 1).value
        
        # Record retry attempt in database
        self._record_retry_attempt(task_id, retry_state.attempts[-1])
        
        logger.info(f"Task {task_id} scheduled for retry {retry_state.current_attempt} at {retry_at} "
                   f"(error_type={error_type}, next_model={next_model})")
        
        return True, retry_at, next_model
    
    def handle_task_success(self, task_id: str, task_type: TaskType, 
                          model_tier: str = None) -> None:
        """Handle successful task completion.
        
        Args:
            task_id: Successful task ID
            task_type: Type of task
            model_tier: Model tier used
        """
        # Reset circuit breaker (thread-safe)
        cb_key = f"{task_type.value}:{model_tier or 'default'}"
        with self._lock:
            if cb_key in self._circuit_breakers:
                self._circuit_breakers[cb_key].record_success()
                self._save_circuit_breaker_state(cb_key)
            
            # Clean up retry state
            if task_id in self._retry_states:
                del self._retry_states[task_id]
        
        logger.debug(f"Task {task_id} completed successfully with {model_tier}")
    
    def _get_retry_state(self, task_id: str, task_type: TaskType, 
                        original_model: str) -> TaskRetryState:
        """Get or create retry state for a task."""
        if task_id not in self._retry_states:
            # Build escalation path based on task type
            escalation_paths = {
                TaskType.CONTENT: [ModelTier.HAIKU.value, ModelTier.SONNET.value, ModelTier.OPUS.value],
                TaskType.CODE: [ModelTier.SONNET.value, ModelTier.OPUS.value, ModelTier.OPUS.value],
                TaskType.RESEARCH: [ModelTier.HAIKU.value, ModelTier.SONNET.value, ModelTier.OPUS.value],
                TaskType.TRANSLATION: [ModelTier.SONNET.value, ModelTier.OPUS.value, ModelTier.OPUS.value],
                TaskType.REVIEW: [ModelTier.SONNET.value, ModelTier.OPUS.value, ModelTier.OPUS.value],
            }
            
            self._retry_states[task_id] = TaskRetryState(
                task_id=task_id,
                task_type=task_type,
                original_model_tier=original_model,
                max_retries=self.config.retry.max_retries_default,
                escalation_path=escalation_paths.get(task_type, [original_model])
            )
        
        return self._retry_states[task_id]
    
    def _check_circuit_breaker(self, key: str, error_type: ErrorType,
                               increment: bool = True) -> bool:
        """Check if circuit breaker should prevent retry.
        
        Args:
            key: Circuit breaker key (task_type:model_tier)
            error_type: Type of error
            increment: Whether to count this as a new failure. Should be True only
                       for the first failure of each unique task, not for retries,
                       so the CB reflects unique-task failure rate rather than
                       per-attempt failure rate.
        """
        if key not in self._circuit_breakers:
            self._circuit_breakers[key] = self._load_circuit_breaker_state(key)
        
        cb = self._circuit_breakers[key]
        
        # Only record failure for first-time task failures (not retries of the same task)
        if increment:
            cb.record_failure(self.config.retry.circuit_breaker_threshold)
            self._save_circuit_breaker_state(key)
        
        # Check if circuit is open
        is_open = cb.is_open(
            self.config.retry.circuit_breaker_threshold,
            self.config.retry.circuit_breaker_reset_minutes
        )
        
        if is_open:
            logger.warning(f"Circuit breaker {key} is open (failures: {cb.failure_count})")
        
        return is_open
    
    def _load_circuit_breaker_state(self, key: str) -> CircuitBreakerState:
        """Load circuit breaker state from database."""
        # Serialize the read against concurrent circuit-breaker writes. Under a
        # shared-cache in-memory DB (tests / dry-run) table-level locking would
        # otherwise raise ``OperationalError: database table is locked`` here
        # (see db._locked / db.py:153-159). For file DBs this is a no-op.
        with self.db._locked():
            rows = self.db.fetch_all("""
                SELECT * FROM circuit_breaker_state WHERE name = ?
            """, [key])
        row = rows[0] if rows else None
        
        if row:
            opened_at = row['opened_at']
            if isinstance(opened_at, str):
                opened_at = datetime.fromisoformat(opened_at)
            last_failure = row['last_failure']
            if isinstance(last_failure, str):
                last_failure = datetime.fromisoformat(last_failure)
            return CircuitBreakerState(
                name=key,
                failure_count=row['failure_count'],
                last_failure=last_failure,
                opened_at=opened_at,
                state=row['state']
            )
        
        return CircuitBreakerState(name=key)
    
    def _save_circuit_breaker_state(self, key: str) -> None:
        """Save circuit breaker state to database (best-effort, tolerates DB contention)."""
        cb = self._circuit_breakers[key]

        try:
            # Serialize the write through transaction() so it is ordered against
            # concurrent circuit-breaker access (shared-cache table-level locking)
            # and rolls back on failure. Use the yielded conn, not self.db.execute,
            # to avoid re-entering get_connection and double-committing.
            with self.db.transaction() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO circuit_breaker_state (name, failure_count, last_failure, opened_at, state)
                    VALUES (?, ?, ?, ?, ?)
                """, [cb.name, cb.failure_count, cb.last_failure, cb.opened_at, cb.state])
        except sqlite3.OperationalError as e:
            # Best-effort tolerance for transient DB contention only; genuine
            # programming errors (schema drift, etc.) are no longer swallowed.
            logger.warning(f"Failed to persist circuit breaker state for {key}: {e}")
    
    def _record_retry_attempt(self, task_id: str, attempt: RetryAttempt) -> None:
        """Record retry attempt in database (best-effort)."""
        try:
            self.db.execute("""
                INSERT INTO retry_attempts (
                    task_id, attempt_number, scheduled_at, model_tier,
                    error_type, error_message, backoff_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                task_id,
                attempt.attempt_number, attempt.scheduled_at, attempt.model_tier,
                attempt.error_type.value if attempt.error_type else None,
                attempt.error_message, attempt.backoff_seconds
            ])
        except Exception as e:
            logger.warning(f"Failed to persist retry attempt for {task_id}: {e}")
    
    def _record_error_pattern(self, error_message: str, error_type: ErrorType,
                             task_type: TaskType, model_tier: str = None) -> None:
        """Record error pattern for analysis and learning."""
        # Truncate very long error messages
        error_msg = error_message[:500] if error_message else "Unknown error"
        
        try:
            self.db.execute("""
                INSERT INTO error_patterns (error_message, error_type, task_type, model_tier, frequency)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(error_message, task_type, model_tier) DO UPDATE SET
                    frequency = frequency + 1,
                    last_seen = CURRENT_TIMESTAMP
            """, [error_msg, error_type.value, task_type.value, model_tier])
        except Exception as e:
            logger.warning(f"Failed to record error pattern: {e}")
    
    def get_retry_queue(self) -> List[Dict[str, Any]]:
        """Get tasks ready for retry.
        
        Returns:
            List of tasks that should be retried now
        """
        now = now_utc()
        results = self.db.fetch_all("""
            SELECT DISTINCT task_id, MIN(scheduled_at) as next_retry
            FROM retry_attempts 
            WHERE executed_at IS NULL AND scheduled_at <= ?
            GROUP BY task_id
            ORDER BY next_retry ASC
        """, [now])
        
        return [
            {
                "task_id": row["task_id"],
                "scheduled_at": row["next_retry"]
            }
            for row in results
        ]
    
    def mark_retry_executed(self, task_id: str, success: bool) -> None:
        """Mark retry attempt as executed.
        
        Args:
            task_id: Task ID
            success: Whether retry was successful
        """
        self.db.execute("""
            UPDATE retry_attempts 
            SET executed_at = CURRENT_TIMESTAMP, success = ?
            WHERE task_id = ? AND executed_at IS NULL
        """, [success, task_id])
    
    def get_error_statistics(self) -> Dict[str, Any]:
        """Get error and recovery statistics.
        
        Returns:
            Dictionary with error statistics
        """
        # Error type distribution
        error_types = self.db.fetch_all("""
            SELECT error_type, COUNT(*) as count, SUM(frequency) as total_frequency
            FROM error_patterns
            GROUP BY error_type
            ORDER BY total_frequency DESC
        """)
        
        # Circuit breaker status
        circuit_breakers = self.db.fetch_all("""
            SELECT name, failure_count, state, last_failure
            FROM circuit_breaker_state
            WHERE failure_count > 0
        """)
        
        # Retry success rates
        retry_stats = self.db.fetch_all("""
            SELECT 
                COUNT(*) as total_retries,
                SUM(CASE WHEN success THEN 1 ELSE 0 END) as successful_retries,
                AVG(backoff_seconds) as avg_backoff
            FROM retry_attempts
            WHERE executed_at IS NOT NULL
        """)
        
        return {
            "error_type_distribution": [
                {"error_type": row["error_type"], "count": row["count"], "frequency": row["total_frequency"]}
                for row in error_types
            ],
            "circuit_breakers": [
                {
                    "name": row["name"],
                    "failure_count": row["failure_count"],
                    "state": row["state"],
                    "last_failure": row["last_failure"]
                }
                for row in circuit_breakers
            ],
            "retry_statistics": retry_stats[0] if retry_stats else {
                "total_retries": 0,
                "successful_retries": 0,
                "avg_backoff": 0
            }
        }