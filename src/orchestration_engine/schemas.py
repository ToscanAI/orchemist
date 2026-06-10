"""Structured Output Schemas for the Orchestration Engine.

All task inputs, outputs, and metadata follow strict Pydantic schemas for
type safety, validation, and consistent interfaces across all task types.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .timestamps import now_utc

# Core Enums

class Priority(IntEnum):
    """Task priority levels."""
    CRITICAL = 1    # Process immediately, bypass normal queue
    HIGH = 2        # Process before normal priority tasks
    NORMAL = 3      # Standard priority (default)
    LOW = 4         # Process when no higher priority work


class TaskType(str, Enum):
    """Supported task types."""
    CONTENT = "content"
    CODE = "code"
    RESEARCH = "research"
    TRANSLATION = "translation"
    REVIEW = "review"
    # Knowledge-work task types (#123)
    TRIAGE = "triage"
    ANALYSIS = "analysis"
    COMPLIANCE = "compliance"
    FINANCIAL = "financial"
    SALES = "sales"
    SUPPORT = "support"
    COMMAND = "command"
    ACCEPTANCE_RUN = "acceptance_run"


class TaskState(str, Enum):
    """Task lifecycle states."""
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRY = "retry"
    PERMANENTLY_FAILED = "permanently_failed"
    CANCELLED = "cancelled"


class OrchestraState(str, Enum):
    """Orchestra workflow states."""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConfidenceLevel(str, Enum):
    """Human-readable confidence levels."""
    VERY_LOW = "very_low"    # 0.0 - 0.2
    LOW = "low"              # 0.2 - 0.4
    MEDIUM = "medium"        # 0.4 - 0.6
    HIGH = "high"            # 0.6 - 0.8
    VERY_HIGH = "very_high"  # 0.8 - 1.0


class ModelTier(str, Enum):
    """Available model tiers."""
    HAIKU = "haiku-4-5"
    SONNET = "sonnet-4"
    OPUS = "opus-4-6"


# Base Task Input Schema

class TaskSpec(BaseModel):
    """Input specification for submitting a new task."""
    # Auto-generated unique ID (set here so callers can override for testing)
    id: str = Field(default_factory=lambda: str(uuid4()))

    type: TaskType
    payload: Dict[str, Any]
    priority: Priority = Priority.NORMAL

    # Execution tracking
    retry_count: int = 0

    # Orchestra integration
    orchestra_id: Optional[str] = None
    orchestra_phase: Optional[str] = None

    # Retry configuration
    max_retries: int = 3
    timeout_seconds: int = 3600

    # Quality requirements
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    preferred_model: Optional[ModelTier] = None

    # Resource limits
    cost_limit_usd: Optional[Decimal] = None

    # Metadata
    created_by: Optional[str] = None
    tags: List[str] = []


# Task Result Schemas

class TaskError(BaseModel):
    """Structured error information."""
    code: str
    message: str
    severity: Literal["warning", "error", "critical"]
    context: Dict[str, Any] = {}
    suggestion: Optional[str] = None


class TaskResult(BaseModel):
    """Complete task execution result."""
    task_id: str
    task_type: TaskType
    state: TaskState

    # Quality metrics
    confidence: float = Field(ge=0.0, le=1.0, description="Overall quality score")
    confidence_level: ConfidenceLevel = ConfidenceLevel.MEDIUM  # Will be auto-calculated

    # Core result data
    result: Dict[str, Any]  # Task-specific payload

    # Metadata and tracking
    metadata: Dict[str, Any] = {}
    errors: List[TaskError] = []
    warnings: List[str] = []

    # Execution details
    created_at: datetime = Field(default_factory=now_utc)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    model_used: Optional[str] = None
    tokens_consumed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    execution_time_seconds: float = 0.0
    cost_usd: Optional[Decimal] = None

    # Quality gate results
    quality_checks_passed: Dict[str, bool] = {}
    quality_check_details: Dict[str, Any] = {}

    @model_validator(mode='after')
    def set_confidence_level(self):
        """Auto-set confidence level based on numeric confidence."""
        conf = self.confidence

        if conf <= 0.2:
            self.confidence_level = ConfidenceLevel.VERY_LOW
        elif conf <= 0.4:
            self.confidence_level = ConfidenceLevel.LOW
        elif conf <= 0.6:
            self.confidence_level = ConfidenceLevel.MEDIUM
        elif conf <= 0.8:
            self.confidence_level = ConfidenceLevel.HIGH
        else:
            self.confidence_level = ConfidenceLevel.VERY_HIGH

        return self


class TaskStatus(BaseModel):
    """Current status of a task in the queue."""
    task_id: str
    task_type: TaskType
    state: TaskState
    priority: Priority

    # Timestamps
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None

    # Retry tracking
    retry_count: int = 0
    max_retries: int = 3

    # Orchestra integration
    orchestra_id: Optional[str] = None
    orchestra_phase: Optional[str] = None

    # Progress indicators
    progress_message: Optional[str] = None
    progress_percentage: Optional[float] = Field(None, ge=0.0, le=100.0)

    # Resource usage
    tokens_consumed: int = 0
    cost_usd: Optional[Decimal] = None
    execution_time_seconds: float = 0.0


class TaskSummary(BaseModel):
    """Lightweight task summary for listings."""
    task_id: str
    task_type: TaskType
    state: TaskState
    priority: Priority
    created_at: datetime

    # Quick status info
    retry_count: int = 0
    orchestra_id: Optional[str] = None
    progress_percentage: Optional[float] = None

    # Brief description
    title: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = []


# Orchestra Schemas

class OrchestraSpec(BaseModel):
    """Input specification for creating a new orchestra workflow."""
    template: str = ""  # Template name like "content-pipeline", "code-sprint"
    name: Optional[str] = None
    description: Optional[str] = None  # Human-readable description
    phases: List[str] = []             # Ordered list of phase names
    config: Dict[str, Any] = Field(default_factory=dict)  # Template-specific parameters
    priority: Priority = Priority.NORMAL

    # Resource limits
    cost_budget_usd: Optional[Decimal] = None
    time_budget_hours: Optional[int] = None

    # Metadata
    created_by: Optional[str] = None
    tags: List[str] = []


class OrchestraStatus(BaseModel):
    """Current status of an orchestra workflow."""
    orchestra_id: str
    template: str
    name: Optional[str] = None
    state: OrchestraState
    priority: Priority

    # Timestamps
    created_at: datetime
    completed_at: Optional[datetime] = None

    # Progress tracking
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    cancelled_tasks: int = 0

    # Resource usage
    cost_budget_usd: Optional[Decimal] = None
    cost_spent_usd: Decimal = Decimal('0.00')
    time_budget_hours: Optional[int] = None

    # Current phase info
    current_phase: Optional[str] = None
    phase_progress: Optional[float] = Field(None, ge=0.0, le=100.0)

    @property
    def progress_percentage(self) -> float:
        """Calculate overall progress percentage."""
        if self.total_tasks == 0:
            return 0.0
        return (self.completed_tasks / self.total_tasks) * 100.0


# Queue Statistics

class TaskStats(BaseModel):
    """Statistics for a specific task state/type."""
    count: int
    oldest_task_age_seconds: Optional[float] = None
    avg_execution_time_seconds: Optional[float] = None
    total_cost_usd: Decimal = Decimal('0.00')


class QueueStats(BaseModel):
    """Overall queue statistics and health metrics."""
    timestamp: datetime = Field(default_factory=now_utc)

    # Task counts by state
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    retrying: int = 0
    cancelled: int = 0

    # Priority breakdown
    priority_breakdown: Dict[str, int] = {}  # Priority name -> count

    # Type breakdown
    type_breakdown: Dict[str, int] = {}  # Task type -> count

    # Performance metrics
    avg_queue_wait_seconds: Optional[float] = None
    avg_execution_time_seconds: Optional[float] = None
    throughput_tasks_per_hour: Optional[float] = None

    # Resource usage
    total_cost_today_usd: Decimal = Decimal('0.00')
    total_tokens_consumed: int = 0

    # Worker status
    active_workers: int = 0
    max_workers: int = 8

    # Health indicators
    queue_depth_warning: bool = False  # True if queued tasks > 50
    stale_tasks_warning: bool = False  # True if tasks stuck > 30min
    dead_letter_count: int = 0

    @property
    def worker_utilization(self) -> float:
        """Calculate worker utilization percentage."""
        if self.max_workers == 0:
            return 0.0
        return (self.active_workers / self.max_workers) * 100.0

    @property
    def total_tasks(self) -> int:
        """Total tasks across all states."""
        return (self.queued + self.running + self.completed +
                self.failed + self.retrying + self.cancelled)


# Task Filters for Querying

class TaskFilters(BaseModel):
    """Filters for querying tasks."""
    states: Optional[List[TaskState]] = None
    types: Optional[List[TaskType]] = None
    priorities: Optional[List[Priority]] = None
    orchestra_id: Optional[str] = None
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    tags: Optional[List[str]] = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


# Task Run Schema (for individual execution attempts)

class TaskRunResult(BaseModel):
    """Result from a single task execution attempt."""
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    attempt_number: int

    # Execution context
    model: str
    thinking_level: Optional[str] = None
    session_id: Optional[str] = None
    worker_id: Optional[str] = None

    # Timing
    started_at: datetime = Field(default_factory=now_utc)
    completed_at: Optional[datetime] = None

    # Results
    state: TaskState
    result: Optional[Dict[str, Any]] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    error_message: Optional[str] = None
    error_type: Optional[Literal["transient", "permanent", "quality"]] = None

    # Resource usage
    tokens_used: int = 0
    cost_usd: Optional[Decimal] = None
    peak_memory_mb: Optional[int] = None


# Dead Letter Queue Schema

class DeadLetterTask(BaseModel):
    """Task that permanently failed and was moved to dead letter queue."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    original_task_id: str
    task_type: TaskType
    failure_reason: str
    failure_count: int
    payload: Dict[str, Any]  # Original task payload
    created_at: datetime = Field(default_factory=now_utc)

    # Analysis metadata
    error_patterns: List[str] = []
    suggested_fixes: List[str] = []


# Utility Functions

def generate_task_id() -> str:
    """Generate a unique task ID."""
    return str(uuid4())


def generate_orchestra_id() -> str:
    """Generate a unique orchestra ID."""
    return str(uuid4())


def calculate_retry_delay(attempt_number: int) -> int:
    """Calculate exponential backoff delay in seconds."""
    base_delay = 1  # 1 second base
    max_delay = 60  # 1 minute maximum

    delay = min(base_delay * (2 ** (attempt_number - 1)), max_delay)
    return delay


def select_model_tier(task_type: TaskType, attempt_number: int) -> ModelTier:
    """Select model tier with escalation for retries."""
    escalation_paths = {
        TaskType.CONTENT: [ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS],
        TaskType.CODE: [ModelTier.SONNET, ModelTier.OPUS, ModelTier.OPUS],
        TaskType.RESEARCH: [ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS],
        TaskType.TRANSLATION: [ModelTier.SONNET, ModelTier.OPUS, ModelTier.OPUS],
        TaskType.REVIEW: [ModelTier.SONNET, ModelTier.OPUS, ModelTier.OPUS],
        # Knowledge-work types (#123)
        TaskType.TRIAGE: [ModelTier.SONNET, ModelTier.OPUS, ModelTier.OPUS],
        TaskType.ANALYSIS: [ModelTier.SONNET, ModelTier.OPUS, ModelTier.OPUS],
        TaskType.COMPLIANCE: [ModelTier.OPUS, ModelTier.OPUS, ModelTier.OPUS],
        TaskType.FINANCIAL: [ModelTier.OPUS, ModelTier.OPUS, ModelTier.OPUS],
        TaskType.SALES: [ModelTier.SONNET, ModelTier.OPUS, ModelTier.OPUS],
        TaskType.SUPPORT: [ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS],
        TaskType.COMMAND: [ModelTier.HAIKU, ModelTier.HAIKU, ModelTier.HAIKU],
    }

    path = escalation_paths.get(task_type, [ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS])
    index = min(attempt_number - 1, len(path) - 1)
    return path[index]


# Default max retries per task type
DEFAULT_MAX_RETRIES = {
    TaskType.CONTENT: 3,
    TaskType.CODE: 2,
    TaskType.RESEARCH: 3,
    TaskType.TRANSLATION: 4,
    TaskType.REVIEW: 2,
    # Knowledge-work types (#123)
    TaskType.TRIAGE: 3,
    TaskType.ANALYSIS: 3,
    TaskType.COMPLIANCE: 2,
    TaskType.FINANCIAL: 2,
    TaskType.SALES: 3,
    TaskType.SUPPORT: 3,
    TaskType.COMMAND: 1,
}


# Phase 2 Schemas - Task Runner, Workers, Progress, Recovery

class WorkerStatus(BaseModel):
    """Status information for a worker."""
    worker_id: str
    state: Literal["idle", "assigned", "running", "stale", "terminated"]
    assigned_task_id: Optional[str] = None
    session_id: Optional[str] = None
    created_at: datetime
    last_heartbeat: datetime
    last_activity: Optional[str] = None
    heartbeat_age_seconds: float = 0.0

    @property
    def is_active(self) -> bool:
        """Check if worker is actively processing."""
        return self.state in ["assigned", "running"]

    @property
    def is_stale(self) -> bool:
        """Check if worker appears stale."""
        return self.heartbeat_age_seconds > 300  # 5 minutes


class ProgressEvent(BaseModel):
    """Individual progress event for task tracking."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    event_type: Literal[
        "queued", "started", "progress_update", "model_selected",
        "session_created", "session_ended", "retry_scheduled",
        "escalated", "completed", "failed", "cancelled", "timeout",
        "resource_limit", "circuit_breaker"
    ]
    timestamp: datetime = Field(default_factory=now_utc)

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


class CircuitBreakerState(BaseModel):
    """Circuit breaker state for error recovery."""
    name: str                              # Circuit breaker identifier (e.g., "content:haiku")
    failure_count: int = 0                 # Consecutive failures
    last_failure: Optional[datetime] = None # Last failure timestamp
    opened_at: Optional[datetime] = None   # When circuit was opened
    state: Literal["closed", "open", "half_open"] = "closed"

    def is_open(self, threshold: int = 5, reset_timeout_minutes: int = 30) -> bool:
        """Check if circuit breaker is open."""
        if self.state == "open" and self.opened_at:
            # Check if reset timeout has passed
            opened = self.opened_at if self.opened_at.tzinfo else self.opened_at.replace(tzinfo=timezone.utc)
            reset_time = opened + timedelta(minutes=reset_timeout_minutes)
            if now_utc() >= reset_time:
                return False  # Allow half-open state
            return True

        return self.failure_count >= threshold

    @property
    def can_execute(self) -> bool:
        """Check if circuit allows execution."""
        return self.state in ["closed", "half_open"]


class TaskExecutionRequest(BaseModel):
    """Request to execute a specific task immediately."""
    task_id: str
    force: bool = False          # Force execution even if worker pool is full
    model_tier: Optional[str] = None    # Override model tier
    timeout_override: Optional[int] = None  # Override timeout seconds


class WorkerPoolStatus(BaseModel):
    """Comprehensive worker pool status."""
    total_workers: int
    active_workers: int
    idle_workers: int
    stale_workers: int
    max_workers: int

    # Resource utilization
    worker_utilization: float = Field(ge=0.0, le=100.0)  # Percentage
    session_utilization: float = Field(ge=0.0, le=100.0)  # Percentage

    # Resource limits
    current_sessions: int
    max_sessions: int
    daily_cost_usd: float
    daily_budget_usd: Optional[float] = None
    budget_utilization: float = Field(default=0.0, ge=0.0, le=100.0)

    # Worker details by state
    workers_by_state: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)

    @property
    def available_capacity(self) -> int:
        """Number of tasks that can be assigned immediately."""
        return max(0, min(
            self.max_workers - self.active_workers,
            self.max_sessions - self.current_sessions
        ))


class RunnerStatus(BaseModel):
    """Comprehensive task runner status."""
    running: bool
    uptime_seconds: Optional[float] = None

    # Component status
    worker_pool_status: WorkerPoolStatus
    queue_depth: int
    active_tasks: int
    pending_retries: int

    # Performance metrics
    tasks_completed_today: int = 0
    tasks_failed_today: int = 0
    avg_execution_time_seconds: Optional[float] = None
    throughput_tasks_per_hour: Optional[float] = None

    # Error recovery status
    circuit_breakers_open: int = 0
    total_retries_today: int = 0
    retry_success_rate: Optional[float] = None

    # Resource usage
    total_cost_today_usd: float = 0.0
    total_tokens_consumed_today: int = 0

    @property
    def health_status(self) -> Literal["healthy", "degraded", "unhealthy"]:
        """Overall health assessment."""
        if not self.running:
            return "unhealthy"

        # Check various health indicators
        if (self.circuit_breakers_open > 3 or
            self.worker_pool_status.worker_utilization > 90 or
            (self.retry_success_rate and self.retry_success_rate < 0.5)):
            return "degraded"

        return "healthy"


# Configuration Import (from config.py)
# Note: EngineConfig is defined in config.py to avoid circular imports
# Import it when needed: from .config import EngineConfig

# Re-export PhaseOutcome and determine_outcome from transitions so callers can
# do ``from orchestration_engine.schemas import PhaseOutcome`` without needing
# to know which module owns the definition.
from .transitions import PhaseOutcome, determine_outcome  # noqa: F401  re-export


class PipelineConfig(BaseModel):
    """Typed container for pipeline configuration values.

    Used by sequencer and executor components to carry pipeline-level
    configuration through the execution chain.  Fields are intentionally
    open (``extra="allow"``) so any config_schema property from a pipeline
    YAML can be stored without schema changes.
    """

    model_config = {"extra": "allow"}

    repo_path: Optional[str] = None
    branch_name: Optional[str] = None
    issue_number: Optional[int] = None
    issue_title: Optional[str] = None
    issue_body: Optional[str] = None
    repo_url: Optional[str] = None
