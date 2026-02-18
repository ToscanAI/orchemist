# Error Recovery and Retry Logic

The orchestration engine implements **robust error recovery mechanisms** with intelligent retry strategies, model tier escalation, and comprehensive failure analysis to ensure maximum task completion rates.

## Error Recovery Architecture

```ascii
┌─────────────────────────────────────────────────────────────────┐
│                    ERROR RECOVERY SYSTEM                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │ Error       │  │ Retry       │  │ Escalation  │            │
│  │ Detection   │  │ Strategy    │  │ Engine      │            │
│  │ & Classification│ Engine    │  │             │            │
│  │             │  │             │  │• Model      │            │
│  │• Transient  │  │• Exp Backoff│  │  Tiers      │            │
│  │• Permanent  │  │• Max Retries│  │• Human      │            │
│  │• Quality    │  │• Circuit    │  │  Escalation │            │
│  │• Resource   │  │  Breaker    │  │• Fallback   │            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
│         │                  │                  │                │
│         ▼                  ▼                  ▼                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              FAILURE HANDLING                           │   │
│  │                                                         │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │   │
│  │  │ Dead Letter │  │ Failure     │  │ Recovery    │    │   │
│  │  │ Queue       │  │ Analysis    │  │ Patterns    │    │   │
│  │  │             │  │             │  │             │    │   │
│  │  │• Permanent  │  │• Root Cause │  │• Success    │    │   │
│  │  │  Failures   │  │• Pattern    │  │  Factors    │    │   │
│  │  │• Manual     │  │  Detection  │  │• Best       │    │   │
│  │  │  Review     │  │• Learning   │  │  Practices  │    │   │
│  │  │• Archive    │  │• Prevention │  │• Prevention │    │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Error Classification System

### Error Types and Handling

```python
from enum import Enum
from typing import Optional, Dict, List, Any, Callable
from datetime import datetime, timedelta
import json

class ErrorType(str, Enum):
    TRANSIENT = "transient"        # Temporary issues, retry with backoff
    PERMANENT = "permanent"        # Fundamental issues, don't retry
    QUALITY = "quality"           # Low quality output, escalate model
    RESOURCE = "resource"         # Resource constraints, wait and retry
    TIMEOUT = "timeout"           # Execution timeout, retry with more time
    RATE_LIMIT = "rate_limit"     # API rate limiting, backoff and retry
    CONFIGURATION = "configuration" # Config issues, needs manual fix
    DEPENDENCY = "dependency"      # Dependent service down, retry later

class ErrorSeverity(str, Enum):
    LOW = "low"                   # Minor issues, log and continue
    MEDIUM = "medium"             # Significant issues, retry with escalation
    HIGH = "high"                 # Major issues, immediate escalation
    CRITICAL = "critical"         # System-threatening, alert and escalate

class TaskError(BaseModel):
    """Detailed error information."""
    error_id: str
    task_id: str
    error_type: ErrorType
    severity: ErrorSeverity
    
    # Error details
    message: str
    exception_type: Optional[str] = None
    stack_trace: Optional[str] = None
    
    # Context
    model_used: str
    thinking_level: str
    attempt_number: int
    
    # Recovery info
    is_retryable: bool
    suggested_action: str
    escalation_required: bool
    
    # Metadata
    occurred_at: datetime = Field(default_factory=datetime.now)
    context: Dict[str, Any] = {}
    
class ErrorClassifier:
    """Classifies errors and determines recovery strategies."""
    
    def __init__(self):
        self.classification_rules = self._setup_classification_rules()
        
    def classify_error(
        self, 
        error_message: str, 
        exception_type: Optional[str] = None,
        context: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Classify an error and determine recovery strategy."""
        context = context or {}
        
        # Apply classification rules
        for rule in self.classification_rules:
            if rule['condition'](error_message, exception_type, context):
                return {
                    'error_type': rule['error_type'],
                    'severity': rule['severity'],
                    'is_retryable': rule['retryable'],
                    'suggested_action': rule['action'],
                    'escalation_required': rule['escalate'],
                    'retry_delay': rule.get('retry_delay', 0),
                    'max_retries': rule.get('max_retries', 3)
                }
                
        # Default classification for unknown errors
        return {
            'error_type': ErrorType.TRANSIENT,
            'severity': ErrorSeverity.MEDIUM,
            'is_retryable': True,
            'suggested_action': 'Retry with exponential backoff',
            'escalation_required': False,
            'retry_delay': 1,
            'max_retries': 3
        }
        
    def _setup_classification_rules(self) -> List[Dict[str, Any]]:
        """Define error classification rules."""
        return [
            # Network and connectivity errors
            {
                'condition': lambda msg, exc, ctx: any(keyword in msg.lower() for keyword in 
                    ['connection', 'network', 'timeout', 'unreachable', 'dns']),
                'error_type': ErrorType.TRANSIENT,
                'severity': ErrorSeverity.LOW,
                'retryable': True,
                'action': 'Retry with exponential backoff',
                'escalate': False,
                'retry_delay': 2,
                'max_retries': 5
            },
            
            # Rate limiting errors
            {
                'condition': lambda msg, exc, ctx: any(keyword in msg.lower() for keyword in 
                    ['rate limit', 'quota exceeded', '429', 'too many requests']),
                'error_type': ErrorType.RATE_LIMIT,
                'severity': ErrorSeverity.MEDIUM,
                'retryable': True,
                'action': 'Backoff with increasing delays',
                'escalate': False,
                'retry_delay': 60,  # Start with 1 minute
                'max_retries': 10
            },
            
            # Model/API errors
            {
                'condition': lambda msg, exc, ctx: any(keyword in msg.lower() for keyword in 
                    ['invalid input', 'malformed', 'bad request', '400']),
                'error_type': ErrorType.PERMANENT,
                'severity': ErrorSeverity.HIGH,
                'retryable': False,
                'action': 'Fix input validation, manual review required',
                'escalate': True,
                'max_retries': 0
            },
            
            # Quality-related errors
            {
                'condition': lambda msg, exc, ctx: any(keyword in msg.lower() for keyword in 
                    ['low confidence', 'quality gate failed', 'validation failed']),
                'error_type': ErrorType.QUALITY,
                'severity': ErrorSeverity.MEDIUM,
                'retryable': True,
                'action': 'Escalate to higher-tier model',
                'escalate': True,
                'retry_delay': 1,
                'max_retries': 3
            },
            
            # Resource constraints
            {
                'condition': lambda msg, exc, ctx: any(keyword in msg.lower() for keyword in 
                    ['out of memory', 'resource exhausted', 'capacity exceeded']),
                'error_type': ErrorType.RESOURCE,
                'severity': ErrorSeverity.HIGH,
                'retryable': True,
                'action': 'Wait for resources, reduce load',
                'escalate': True,
                'retry_delay': 300,  # 5 minutes
                'max_retries': 3
            },
            
            # Timeout errors
            {
                'condition': lambda msg, exc, ctx: any(keyword in msg.lower() for keyword in 
                    ['timeout', 'deadline exceeded', 'execution too long']),
                'error_type': ErrorType.TIMEOUT,
                'severity': ErrorSeverity.MEDIUM,
                'retryable': True,
                'action': 'Retry with increased timeout',
                'escalate': False,
                'retry_delay': 5,
                'max_retries': 2
            },
            
            # Configuration errors
            {
                'condition': lambda msg, exc, ctx: any(keyword in msg.lower() for keyword in 
                    ['config', 'misconfigured', 'invalid setting', 'missing parameter']),
                'error_type': ErrorType.CONFIGURATION,
                'severity': ErrorSeverity.HIGH,
                'retryable': False,
                'action': 'Fix configuration, manual intervention required',
                'escalate': True,
                'max_retries': 0
            }
        ]
```

## Retry Strategy Engine

### Exponential Backoff Implementation

```python
import random
import asyncio
from typing import Union

class RetryStrategy:
    """Configurable retry strategy with multiple backoff algorithms."""
    
    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 300.0,
        backoff_factor: float = 2.0,
        jitter: bool = True
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.jitter = jitter
        
    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number."""
        if attempt <= 0:
            return 0.0
            
        # Exponential backoff
        delay = self.base_delay * (self.backoff_factor ** (attempt - 1))
        
        # Cap at maximum delay
        delay = min(delay, self.max_delay)
        
        # Add jitter to prevent thundering herd
        if self.jitter:
            jitter_range = delay * 0.1  # 10% jitter
            delay += random.uniform(-jitter_range, jitter_range)
            
        return max(0.0, delay)
        
    def should_retry(self, attempt: int, error: TaskError) -> bool:
        """Determine if task should be retried."""
        if attempt > self.max_retries:
            return False
            
        if not error.is_retryable:
            return False
            
        # Special handling for different error types
        if error.error_type == ErrorType.RATE_LIMIT:
            return attempt <= 10  # More retries for rate limits
        elif error.error_type == ErrorType.PERMANENT:
            return False
        elif error.error_type == ErrorType.RESOURCE:
            return attempt <= 5  # Fewer retries for resource issues
            
        return True

class ModelTierEscalation:
    """Handles model tier escalation for retry strategies."""
    
    def __init__(self):
        self.model_tiers = {
            'haiku-4-5': {'tier': 1, 'cost_multiplier': 1.0, 'capability': 'basic'},
            'sonnet-4': {'tier': 2, 'cost_multiplier': 5.0, 'capability': 'advanced'},
            'opus-4-6': {'tier': 3, 'cost_multiplier': 15.0, 'capability': 'expert'}
        }
        
        self.escalation_paths = {
            'code': ['sonnet-4', 'opus-4-6', 'opus-4-6'],  # Start higher for code
            'content': ['haiku-4-5', 'sonnet-4', 'opus-4-6'],
            'research': ['haiku-4-5', 'sonnet-4', 'opus-4-6'],
            'translation': ['sonnet-4', 'opus-4-6', 'opus-4-6'],  # Quality critical
            'review': ['sonnet-4', 'opus-4-6', 'opus-4-6']
        }
        
    def get_next_model(
        self, 
        current_model: str, 
        task_type: str, 
        attempt: int,
        error_type: ErrorType
    ) -> Optional[str]:
        """Get next model tier for escalation."""
        
        # Get escalation path for task type
        path = self.escalation_paths.get(task_type, ['haiku-4-5', 'sonnet-4', 'opus-4-6'])
        
        # For quality errors, escalate immediately
        if error_type == ErrorType.QUALITY:
            current_tier = self.model_tiers.get(current_model, {}).get('tier', 1)
            for model, info in self.model_tiers.items():
                if info['tier'] > current_tier:
                    return model
                    
        # For other errors, follow escalation path
        if attempt - 1 < len(path):
            return path[attempt - 1]
            
        # If we've exhausted the path, use the highest tier
        return path[-1] if path else current_model
        
    def should_escalate(self, error: TaskError, attempt: int) -> bool:
        """Determine if model should be escalated."""
        # Always escalate for quality issues
        if error.error_type == ErrorType.QUALITY:
            return True
            
        # Escalate for repeated failures
        if attempt >= 2 and error.error_type in [ErrorType.TRANSIENT, ErrorType.TIMEOUT]:
            return True
            
        # Escalate for high-severity errors
        if error.severity in [ErrorSeverity.HIGH, ErrorSeverity.CRITICAL]:
            return True
            
        return False

class RetryEngine:
    """Main retry engine that coordinates retry strategies."""
    
    def __init__(self, task_queue, metrics_collector):
        self.task_queue = task_queue
        self.metrics_collector = metrics_collector
        self.error_classifier = ErrorClassifier()
        self.retry_strategy = RetryStrategy()
        self.model_escalation = ModelTierEscalation()
        
    async def handle_task_failure(
        self, 
        task_id: str, 
        error_message: str,
        exception_type: Optional[str] = None,
        context: Dict[str, Any] = None
    ) -> bool:
        """Handle task failure and determine retry strategy."""
        
        # Get task information
        task = await self.task_queue.get_task(task_id)
        if not task:
            return False
            
        # Classify the error
        error_classification = self.error_classifier.classify_error(
            error_message, exception_type, context
        )
        
        # Create error record
        error = TaskError(
            error_id=f"error_{uuid4().hex[:12]}",
            task_id=task_id,
            error_type=ErrorType(error_classification['error_type']),
            severity=ErrorSeverity(error_classification['severity']),
            message=error_message,
            exception_type=exception_type,
            model_used=task.model_used,
            thinking_level=task.thinking_level,
            attempt_number=task.retry_count + 1,
            is_retryable=error_classification['is_retryable'],
            suggested_action=error_classification['suggested_action'],
            escalation_required=error_classification['escalation_required'],
            context=context or {}
        )
        
        # Store error for analysis
        await self._store_error(error)
        
        # Record failure metrics
        self.metrics_collector.record_task_failure(task_id, error)
        
        # Determine if we should retry
        should_retry = self.retry_strategy.should_retry(task.retry_count + 1, error)
        
        if not should_retry:
            # Move to dead letter queue
            await self._move_to_dead_letter_queue(task, error)
            return False
            
        # Calculate retry delay
        retry_delay = self.retry_strategy.calculate_delay(task.retry_count + 1)
        
        # Determine if model should be escalated
        should_escalate = self.model_escalation.should_escalate(error, task.retry_count + 1)
        
        new_model = task.model_used
        if should_escalate:
            new_model = self.model_escalation.get_next_model(
                task.model_used,
                task.task_type,
                task.retry_count + 1,
                error.error_type
            )
            
        # Schedule retry
        await self._schedule_retry(task, error, retry_delay, new_model)
        
        return True
        
    async def _store_error(self, error: TaskError):
        """Store error information for analysis."""
        await self.task_queue.db.execute("""
            INSERT INTO task_errors (
                error_id, task_id, error_type, severity,
                message, exception_type, stack_trace,
                model_used, thinking_level, attempt_number,
                is_retryable, suggested_action, escalation_required,
                occurred_at, context
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            error.error_id, error.task_id, error.error_type.value,
            error.severity.value, error.message, error.exception_type,
            error.stack_trace, error.model_used, error.thinking_level,
            error.attempt_number, error.is_retryable, error.suggested_action,
            error.escalation_required, error.occurred_at,
            json.dumps(error.context)
        ))
        
    async def _schedule_retry(
        self, 
        task: 'Task', 
        error: TaskError, 
        delay: float, 
        new_model: str
    ):
        """Schedule task for retry."""
        retry_time = datetime.now() + timedelta(seconds=delay)
        
        await self.task_queue.db.execute("""
            UPDATE tasks 
            SET status = 'retry',
                retry_count = retry_count + 1,
                next_retry_at = ?,
                preferred_model = ?
            WHERE id = ?
        """, (retry_time, new_model, task.id))
        
        # Log retry scheduling
        print(f"Task {task.id} scheduled for retry at {retry_time} with model {new_model}")
        
    async def _move_to_dead_letter_queue(self, task: 'Task', final_error: TaskError):
        """Move permanently failed task to dead letter queue."""
        await self.task_queue.db.execute("""
            INSERT INTO dead_letter_queue (
                original_task_id, task_type, failure_reason,
                failure_count, payload, final_error,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            task.id, task.task_type, final_error.message,
            task.retry_count + 1, json.dumps(task.payload),
            json.dumps(final_error.dict()), datetime.now()
        ))
        
        # Update task status
        await self.task_queue.db.execute("""
            UPDATE tasks 
            SET status = 'permanently_failed',
                completed_at = ?
            WHERE id = ?
        """, (datetime.now(), task.id))
```

## Circuit Breaker Pattern

### Circuit Breaker Implementation

```python
from enum import Enum
import time

class CircuitState(str, Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing fast
    HALF_OPEN = "half_open" # Testing recovery

class CircuitBreaker:
    """Circuit breaker to prevent cascade failures."""
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        
        self.failure_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
        
    def __call__(self, func):
        """Decorator to apply circuit breaker to a function."""
        def wrapper(*args, **kwargs):
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                else:
                    raise Exception("Circuit breaker is OPEN")
                    
            try:
                result = func(*args, **kwargs)
                self._on_success()
                return result
            except self.expected_exception as e:
                self._on_failure()
                raise e
                
        return wrapper
        
    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        return (
            self.last_failure_time and 
            time.time() - self.last_failure_time >= self.recovery_timeout
        )
        
    def _on_success(self):
        """Handle successful call."""
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        
    def _on_failure(self):
        """Handle failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN

class ServiceCircuitBreakers:
    """Manages circuit breakers for different services."""
    
    def __init__(self):
        self.breakers = {}
        
    def get_breaker(self, service_name: str) -> CircuitBreaker:
        """Get or create circuit breaker for service."""
        if service_name not in self.breakers:
            self.breakers[service_name] = CircuitBreaker()
        return self.breakers[service_name]
        
    def apply_breaker(self, service_name: str, func: Callable):
        """Apply circuit breaker to a function."""
        breaker = self.get_breaker(service_name)
        return breaker(func)
```

## Failure Analysis and Learning

### Failure Pattern Detection

```python
class FailureAnalyzer:
    """Analyzes failure patterns to improve error handling."""
    
    def __init__(self, db_connection):
        self.db = db_connection
        
    def analyze_failure_patterns(self, days: int = 7) -> Dict[str, Any]:
        """Analyze failure patterns over specified period."""
        
        # Get failure data
        failures = self.db.execute("""
            SELECT error_type, severity, message, model_used, 
                   task_type, context, occurred_at
            FROM task_errors 
            WHERE occurred_at > datetime('now', '-{} days')
        """.format(days)).fetchall()
        
        if not failures:
            return {"message": "No failures in specified period"}
            
        # Analyze patterns
        analysis = {
            "total_failures": len(failures),
            "failure_by_type": self._count_by_field(failures, 'error_type'),
            "failure_by_severity": self._count_by_field(failures, 'severity'),
            "failure_by_model": self._count_by_field(failures, 'model_used'),
            "failure_by_task_type": self._count_by_field(failures, 'task_type'),
            "top_error_messages": self._get_top_error_messages(failures),
            "temporal_patterns": self._analyze_temporal_patterns(failures),
            "recommendations": self._generate_recommendations(failures)
        }
        
        return analysis
        
    def _count_by_field(self, failures: List[Dict], field: str) -> Dict[str, int]:
        """Count failures by specified field."""
        counts = {}
        for failure in failures:
            value = failure[field]
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))
        
    def _get_top_error_messages(self, failures: List[Dict], limit: int = 10) -> List[Dict]:
        """Get most common error messages."""
        message_counts = {}
        for failure in failures:
            # Normalize message (remove specific IDs, paths, etc.)
            normalized = self._normalize_error_message(failure['message'])
            message_counts[normalized] = message_counts.get(normalized, 0) + 1
            
        top_messages = sorted(
            message_counts.items(), 
            key=lambda x: x[1], 
            reverse=True
        )[:limit]
        
        return [{"message": msg, "count": count} for msg, count in top_messages]
        
    def _normalize_error_message(self, message: str) -> str:
        """Normalize error message by removing specific details."""
        import re
        
        # Remove UUIDs, timestamps, file paths, etc.
        normalized = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 'UUID', message)
        normalized = re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', 'TIMESTAMP', normalized)
        normalized = re.sub(r'/[^\s]+', 'PATH', normalized)
        normalized = re.sub(r'\d+', 'NUMBER', normalized)
        
        return normalized
        
    def _analyze_temporal_patterns(self, failures: List[Dict]) -> Dict[str, Any]:
        """Analyze temporal patterns in failures."""
        from collections import defaultdict
        import datetime
        
        hourly_counts = defaultdict(int)
        daily_counts = defaultdict(int)
        
        for failure in failures:
            dt = datetime.datetime.fromisoformat(failure['occurred_at'])
            hourly_counts[dt.hour] += 1
            daily_counts[dt.weekday()] += 1
            
        return {
            "peak_failure_hour": max(hourly_counts, key=hourly_counts.get) if hourly_counts else None,
            "peak_failure_day": max(daily_counts, key=daily_counts.get) if daily_counts else None,
            "hourly_distribution": dict(hourly_counts),
            "daily_distribution": dict(daily_counts)
        }
        
    def _generate_recommendations(self, failures: List[Dict]) -> List[str]:
        """Generate recommendations based on failure analysis."""
        recommendations = []
        
        # Analyze failure types
        type_counts = self._count_by_field(failures, 'error_type')
        
        if type_counts.get('rate_limit', 0) > 5:
            recommendations.append("Consider implementing more aggressive rate limiting backoff")
            
        if type_counts.get('quality', 0) > 10:
            recommendations.append("Review quality thresholds and consider model escalation tuning")
            
        if type_counts.get('timeout', 0) > 5:
            recommendations.append("Consider increasing default timeout values")
            
        # Analyze model performance
        model_counts = self._count_by_field(failures, 'model_used')
        if model_counts:
            worst_model = max(model_counts, key=model_counts.get)
            if model_counts[worst_model] > len(failures) * 0.4:  # >40% of failures
                recommendations.append(f"Review {worst_model} model performance and consider alternatives")
                
        return recommendations

class FailurePrevention:
    """Proactive failure prevention based on learned patterns."""
    
    def __init__(self, failure_analyzer: FailureAnalyzer):
        self.analyzer = failure_analyzer
        self.prevention_rules = []
        
    def learn_prevention_rules(self):
        """Learn prevention rules from failure patterns."""
        analysis = self.analyzer.analyze_failure_patterns()
        
        # Generate prevention rules based on analysis
        for error_type, count in analysis.get('failure_by_type', {}).items():
            if count > 10:  # Significant number of failures
                rule = self._create_prevention_rule(error_type, analysis)
                if rule:
                    self.prevention_rules.append(rule)
                    
    def _create_prevention_rule(self, error_type: str, analysis: Dict) -> Optional[Dict]:
        """Create prevention rule for specific error type."""
        if error_type == 'rate_limit':
            return {
                'condition': 'requests_per_minute > 50',
                'action': 'increase_backoff_delay',
                'parameters': {'multiplier': 1.5}
            }
        elif error_type == 'timeout':
            return {
                'condition': 'task_duration > average + 2*std_dev',
                'action': 'increase_timeout',
                'parameters': {'factor': 1.2}
            }
        elif error_type == 'quality':
            return {
                'condition': 'confidence < 0.7 and attempt == 1',
                'action': 'escalate_model_immediately',
                'parameters': {'skip_retries': True}
            }
            
        return None
        
    def should_apply_prevention(self, task_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Check if any prevention rules should be applied."""
        applicable_rules = []
        
        for rule in self.prevention_rules:
            if self._evaluate_condition(rule['condition'], task_context):
                applicable_rules.append(rule)
                
        return applicable_rules
        
    def _evaluate_condition(self, condition: str, context: Dict[str, Any]) -> bool:
        """Evaluate prevention rule condition."""
        # This would implement a safe expression evaluator
        # For now, return False as placeholder
        return False
```

## Human Escalation System

### Escalation Manager

```python
class EscalationManager:
    """Manages human escalation for critical failures."""
    
    def __init__(self, notification_system):
        self.notification_system = notification_system
        self.escalation_rules = self._setup_escalation_rules()
        
    def should_escalate_to_human(self, error: TaskError, task: 'Task') -> bool:
        """Determine if error should be escalated to human."""
        
        # Critical severity always escalates
        if error.severity == ErrorSeverity.CRITICAL:
            return True
            
        # Permanent errors with high cost escalate
        if (error.error_type == ErrorType.PERMANENT and 
            hasattr(task, 'cost_limit_usd') and 
            task.cost_limit_usd and 
            task.cost_limit_usd > 5.0):
            return True
            
        # Multiple failures in orchestra escalate
        if hasattr(task, 'orchestra_id') and task.orchestra_id:
            orchestra_failure_count = self._get_orchestra_failure_count(task.orchestra_id)
            if orchestra_failure_count >= 3:
                return True
                
        # Configuration errors always escalate
        if error.error_type == ErrorType.CONFIGURATION:
            return True
            
        return False
        
    async def escalate_to_human(
        self, 
        error: TaskError, 
        task: 'Task',
        priority: str = "normal"
    ):
        """Escalate error to human review."""
        
        escalation_data = {
            "escalation_id": f"esc_{uuid4().hex[:12]}",
            "task_id": task.id,
            "error_id": error.error_id,
            "priority": priority,
            "error_summary": error.message,
            "suggested_actions": self._generate_suggested_actions(error, task),
            "context": {
                "task_type": task.task_type,
                "model_used": error.model_used,
                "attempt_number": error.attempt_number,
                "orchestra_id": getattr(task, 'orchestra_id', None)
            },
            "created_at": datetime.now()
        }
        
        # Store escalation record
        await self._store_escalation(escalation_data)
        
        # Send notification
        await self._send_escalation_notification(escalation_data)
        
    def _generate_suggested_actions(self, error: TaskError, task: 'Task') -> List[str]:
        """Generate suggested actions for human review."""
        actions = [error.suggested_action] if error.suggested_action else []
        
        if error.error_type == ErrorType.CONFIGURATION:
            actions.append("Review and fix configuration settings")
            actions.append("Test configuration in development environment")
            
        elif error.error_type == ErrorType.PERMANENT:
            actions.append("Analyze input data and fix validation issues")
            actions.append("Consider alternative approach or manual processing")
            
        elif error.error_type == ErrorType.QUALITY:
            actions.append("Review quality thresholds")
            actions.append("Examine model selection for task type")
            actions.append("Consider human review of output")
            
        return actions
        
    async def _store_escalation(self, escalation_data: Dict[str, Any]):
        """Store escalation record."""
        # This would store in database
        pass
        
    async def _send_escalation_notification(self, escalation_data: Dict[str, Any]):
        """Send escalation notification to humans."""
        message = f"""
🚨 Task Escalation Required

**Task ID**: {escalation_data['task_id']}
**Priority**: {escalation_data['priority'].upper()}
**Error**: {escalation_data['error_summary']}

**Context**:
- Task Type: {escalation_data['context']['task_type']}
- Model Used: {escalation_data['context']['model_used']}
- Attempt: {escalation_data['context']['attempt_number']}

**Suggested Actions**:
{chr(10).join(f'• {action}' for action in escalation_data['suggested_actions'])}

Please review and take appropriate action.
"""

        await self.notification_system.send_urgent_notification(
            title="Task Escalation Required",
            message=message,
            priority="high"
        )
```

## Error Recovery Database Schema

```sql
-- Task errors table
CREATE TABLE task_errors (
    error_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    error_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    
    message TEXT NOT NULL,
    exception_type TEXT,
    stack_trace TEXT,
    
    model_used TEXT NOT NULL,
    thinking_level TEXT,
    attempt_number INTEGER NOT NULL,
    
    is_retryable BOOLEAN NOT NULL,
    suggested_action TEXT,
    escalation_required BOOLEAN DEFAULT FALSE,
    
    occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    context TEXT DEFAULT '{}',  -- JSON
    
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

-- Human escalations table
CREATE TABLE human_escalations (
    escalation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    error_id TEXT NOT NULL,
    
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, in_review, resolved
    
    escalated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    assigned_to TEXT,
    resolved_at TIMESTAMP,
    resolution_notes TEXT,
    
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(error_id) REFERENCES task_errors(error_id)
);

-- Circuit breaker states
CREATE TABLE circuit_breaker_states (
    service_name TEXT PRIMARY KEY,
    state TEXT NOT NULL,  -- closed, open, half_open
    failure_count INTEGER DEFAULT 0,
    last_failure_at TIMESTAMP,
    last_success_at TIMESTAMP
);

-- Indexes for performance
CREATE INDEX idx_task_errors_task ON task_errors(task_id, occurred_at DESC);
CREATE INDEX idx_task_errors_type ON task_errors(error_type, severity, occurred_at DESC);
CREATE INDEX idx_escalations_status ON human_escalations(status, priority, escalated_at);
```

The error recovery system provides **comprehensive, intelligent failure handling** that maximizes task completion rates through smart retry strategies, model escalation, and human escalation when necessary.