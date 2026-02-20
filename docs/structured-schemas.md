# Structured Output Schemas

All data in the orchestration engine flows through **strict, validated Pydantic V2 schemas** defined in `schemas.py`. This ensures type safety, consistent interfaces, and reliable quality assessment.

## Pydantic V2 Migration Notes

The codebase uses **Pydantic V2** API throughout:

| Pydantic V1 | Pydantic V2 (used here) |
|-------------|------------------------|
| `@validator` | `@field_validator` |
| `@root_validator` | `@model_validator(mode='after')` |
| `.dict()` | `.model_dump()` |
| `class Config:` | `model_config = ConfigDict(...)` |
| `from pydantic import validator` | `from pydantic import field_validator, model_validator` |

## Core Enums

```python
class Priority(IntEnum):
    CRITICAL = 1 | HIGH = 2 | NORMAL = 3 | LOW = 4

class TaskType(str, Enum):
    CONTENT = "content" | CODE = "code" | RESEARCH = "research"
    TRANSLATION = "translation" | REVIEW = "review"

class TaskState(str, Enum):
    QUEUED = "queued" | RUNNING = "running" | SUCCESS = "success"
    FAILED = "failed" | RETRY = "retry" | PERMANENTLY_FAILED = "permanently_failed"
    CANCELLED = "cancelled"

class OrchestraState(str, Enum):
    RUNNING = "running" | COMPLETED = "completed" | FAILED = "failed" | CANCELLED = "cancelled"

class ConfidenceLevel(str, Enum):
    VERY_LOW = "very_low"   # 0.0–0.2
    LOW = "low"             # 0.2–0.4
    MEDIUM = "medium"       # 0.4–0.6
    HIGH = "high"           # 0.6–0.8
    VERY_HIGH = "very_high" # 0.8–1.0

class ModelTier(str, Enum):
    HAIKU  = "haiku-4-5"
    SONNET = "sonnet-4"
    OPUS   = "opus-4-6"
```

## Task Input / Output

### `TaskSpec` — Task submission

```python
class TaskSpec(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))  # auto-generated
    type: TaskType
    payload: Dict[str, Any]
    priority: Priority = Priority.NORMAL
    
    # Execution
    retry_count: int = 0
    max_retries: int = 3
    timeout_seconds: int = 3600
    
    # Orchestra integration
    orchestra_id: Optional[str] = None
    orchestra_phase: Optional[str] = None
    
    # Quality requirements
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    preferred_model: Optional[ModelTier] = None
    
    # Resource limits
    cost_limit_usd: Optional[Decimal] = None
    
    # Metadata
    created_by: Optional[str] = None
    tags: List[str] = []
```

### `TaskError` — Structured error info

```python
class TaskError(BaseModel):
    code: str
    message: str
    severity: Literal["warning", "error", "critical"]
    context: Dict[str, Any] = {}
    suggestion: Optional[str] = None
```

### `TaskResult` — Execution result

Confidence level is auto-set via `@model_validator(mode='after')`.

```python
class TaskResult(BaseModel):
    task_id: str
    task_type: TaskType
    state: TaskState
    
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_level: ConfidenceLevel = ConfidenceLevel.MEDIUM  # auto-calculated
    
    result: Dict[str, Any]     # task-specific payload
    metadata: Dict[str, Any] = {}
    errors: List[TaskError] = []
    warnings: List[str] = []
    
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    model_used: Optional[str] = None
    tokens_consumed: int = 0
    execution_time_seconds: float = 0.0
    cost_usd: Optional[Decimal] = None
    
    quality_checks_passed: Dict[str, bool] = {}
    quality_check_details: Dict[str, Any] = {}
    
    @model_validator(mode='after')
    def set_confidence_level(self):
        # Maps confidence float → ConfidenceLevel enum
        ...
```

### `TaskStatus` — Queue status (polling)

```python
class TaskStatus(BaseModel):
    task_id: str
    task_type: TaskType
    state: TaskState
    priority: Priority
    
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    
    retry_count: int = 0
    max_retries: int = 3
    
    orchestra_id: Optional[str] = None
    orchestra_phase: Optional[str] = None
    
    progress_message: Optional[str] = None
    progress_percentage: Optional[float] = Field(None, ge=0.0, le=100.0)
    
    tokens_consumed: int = 0
    cost_usd: Optional[Decimal] = None
    execution_time_seconds: float = 0.0
```

### `TaskSummary` — Lightweight listing

```python
class TaskSummary(BaseModel):
    task_id: str
    task_type: TaskType
    state: TaskState
    priority: Priority
    created_at: datetime
    retry_count: int = 0
    orchestra_id: Optional[str] = None
    progress_percentage: Optional[float] = None
    title: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = []
```

## Orchestra Schemas

```python
class OrchestraSpec(BaseModel):
    template: str = ""
    name: Optional[str] = None
    description: Optional[str] = None
    phases: List[str] = []
    config: Dict[str, Any] = {}
    priority: Priority = Priority.NORMAL
    cost_budget_usd: Optional[Decimal] = None
    time_budget_hours: Optional[int] = None
    created_by: Optional[str] = None
    tags: List[str] = []

class OrchestraStatus(BaseModel):
    orchestra_id: str
    template: str
    state: OrchestraState
    priority: Priority
    created_at: datetime
    completed_at: Optional[datetime] = None
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    cancelled_tasks: int = 0
    cost_budget_usd: Optional[Decimal] = None
    cost_spent_usd: Decimal = Decimal('0.00')
    current_phase: Optional[str] = None
    
    @property
    def progress_percentage(self) -> float: ...
```

## Queue Statistics

```python
class QueueStats(BaseModel):
    timestamp: datetime
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    retrying: int = 0
    cancelled: int = 0
    
    priority_breakdown: Dict[str, int] = {}  # priority name → count
    type_breakdown: Dict[str, int] = {}       # task type → count
    
    avg_queue_wait_seconds: Optional[float] = None
    avg_execution_time_seconds: Optional[float] = None
    throughput_tasks_per_hour: Optional[float] = None
    
    total_cost_today_usd: Decimal = Decimal('0.00')
    total_tokens_consumed: int = 0
    
    active_workers: int = 0
    max_workers: int = 8
    
    queue_depth_warning: bool = False   # queued > 50
    stale_tasks_warning: bool = False   # running > 30min
    dead_letter_count: int = 0
    
    @property
    def worker_utilization(self) -> float: ...
    @property
    def total_tasks(self) -> int: ...
```

## Dead Letter Queue

```python
class DeadLetterTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    original_task_id: str
    task_type: TaskType
    failure_reason: str
    failure_count: int
    payload: Dict[str, Any]
    created_at: datetime = Field(default_factory=datetime.now)
    error_patterns: List[str] = []
    suggested_fixes: List[str] = []
```

## Filtering

```python
class TaskFilters(BaseModel):
    states: Optional[List[TaskState]] = None
    types: Optional[List[TaskType]] = None
    priorities: Optional[List[Priority]] = None
    orchestra_id: Optional[str] = None
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    tags: Optional[List[str]] = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
```

## Worker & Runner Schemas

These are used by the runner and concurrency modules:

```python
class WorkerStatus(BaseModel):
    worker_id: str
    state: Literal["idle", "assigned", "running", "stale", "terminated"]
    assigned_task_id: Optional[str] = None
    session_id: Optional[str] = None
    created_at: datetime
    last_heartbeat: datetime
    heartbeat_age_seconds: float = 0.0
    
    @property
    def is_active(self) -> bool: ...
    @property
    def is_stale(self) -> bool: ...  # heartbeat_age_seconds > 300

class WorkerPoolStatus(BaseModel):
    total_workers: int
    active_workers: int
    idle_workers: int
    stale_workers: int
    max_workers: int
    worker_utilization: float      # percent
    session_utilization: float     # percent
    current_sessions: int
    max_sessions: int
    daily_cost_usd: float
    daily_budget_usd: Optional[float] = None
    budget_utilization: float = 0.0
    workers_by_state: Dict[str, List[Dict[str, Any]]] = {}
    
    @property
    def available_capacity(self) -> int: ...

class RunnerStatus(BaseModel):
    running: bool
    uptime_seconds: Optional[float] = None
    worker_pool_status: WorkerPoolStatus
    queue_depth: int
    active_tasks: int
    pending_retries: int
    circuit_breakers_open: int = 0
    total_retries_today: int = 0
    retry_success_rate: Optional[float] = None
    total_cost_today_usd: float = 0.0
    
    @property
    def health_status(self) -> Literal["healthy", "degraded", "unhealthy"]: ...
```

## Progress Schema

```python
class ProgressEvent(BaseModel):
    model_config = ConfigDict(...)    # Pydantic V2 style
    
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    event_type: ProgressEventType    # enum
    timestamp: datetime = Field(default_factory=datetime.now)
    message: Optional[str] = None
    progress_percentage: Optional[float] = Field(None, ge=0.0, le=100.0)
    details: Dict[str, Any] = Field(default_factory=dict)
    worker_id: Optional[str] = None
    session_id: Optional[str] = None
    model_tier: Optional[str] = None
    attempt_number: int = 1
    tokens_used: Optional[int] = None
    cost_usd: Optional[str] = None   # Decimal as string for JSON
    memory_mb: Optional[int] = None
```

## Circuit Breaker Schema

Used in both `schemas.py` and `recovery.py` (as a dataclass):

```python
class CircuitBreakerState(BaseModel):
    name: str                              # e.g. "content:haiku-4-5"
    failure_count: int = 0
    last_failure: Optional[datetime] = None
    opened_at: Optional[datetime] = None
    state: Literal["closed", "open", "half_open"] = "closed"
    
    def is_open(self, threshold: int = 5, reset_timeout_minutes: int = 30) -> bool: ...
    @property
    def can_execute(self) -> bool: ...
```

## Utility Functions

```python
def generate_task_id() -> str: ...
def generate_orchestra_id() -> str: ...

def calculate_retry_delay(attempt_number: int) -> int:
    """Exponential backoff: min(1 * 2^(n-1), 60) seconds."""

def select_model_tier(task_type: TaskType, attempt_number: int) -> ModelTier:
    """Returns model for given task type and attempt number (with escalation)."""

DEFAULT_MAX_RETRIES: Dict[TaskType, int]  # per-task-type defaults
```
