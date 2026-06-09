"""Tests for the schemas module.

Tests Pydantic model validation, serialization/deserialization,
and utility functions for the orchestration engine schemas.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from orchestration_engine.schemas import (
    # Enums
    Priority, TaskType, TaskState, OrchestraState, ConfidenceLevel, ModelTier,
    # Core schemas
    TaskSpec, TaskStatus, TaskResult, TaskSummary,
    OrchestraSpec, OrchestraStatus, QueueStats, TaskFilters,
    TaskRunResult, DeadLetterTask, TaskError,
    # Utility functions
    generate_task_id, generate_orchestra_id, calculate_retry_delay,
    select_model_tier, DEFAULT_MAX_RETRIES
)


class TestEnums:
    """Test enum definitions and values."""
    
    def test_priority_enum(self):
        """Test Priority enum values and ordering."""
        assert Priority.CRITICAL == 1
        assert Priority.HIGH == 2
        assert Priority.NORMAL == 3
        assert Priority.LOW == 4
        
        # Test ordering
        assert Priority.CRITICAL < Priority.HIGH < Priority.NORMAL < Priority.LOW
    
    def test_task_type_enum(self):
        """Test TaskType enum values."""
        assert TaskType.CONTENT == "content"
        assert TaskType.CODE == "code"
        assert TaskType.RESEARCH == "research"
        assert TaskType.TRANSLATION == "translation"
        assert TaskType.REVIEW == "review"
        
        # Test all expected types are present (original + knowledge-work types from #123 + #532)
        expected_types = {
            "content", "code", "research", "translation", "review",
            "triage", "analysis", "compliance", "financial", "sales", "support",
            "command",
            "acceptance_run",  # added by #532: engine-executed pytest runner phase
        }
        actual_types = {t.value for t in TaskType}
        assert actual_types == expected_types
    
    def test_task_state_enum(self):
        """Test TaskState enum values."""
        expected_states = {
            "queued", "running", "success", "failed", 
            "retry", "permanently_failed", "cancelled"
        }
        actual_states = {s.value for s in TaskState}
        assert actual_states == expected_states
    
    def test_confidence_level_enum(self):
        """Test ConfidenceLevel enum values."""
        expected_levels = {"very_low", "low", "medium", "high", "very_high"}
        actual_levels = {l.value for l in ConfidenceLevel}
        assert actual_levels == expected_levels


class TestTaskSpec:
    """Test TaskSpec input schema."""
    
    def test_minimal_task_spec(self):
        """Test creating task spec with minimal required fields."""
        spec = TaskSpec(
            type=TaskType.CONTENT,
            payload={"message": "Hello world"}
        )
        
        assert spec.type == TaskType.CONTENT
        assert spec.payload == {"message": "Hello world"}
        assert spec.priority == Priority.NORMAL  # Default
        assert spec.max_retries == 3  # Default
        assert spec.timeout_seconds == 3600  # Default
        assert spec.min_confidence == 0.7  # Default
        assert spec.tags == []  # Default
    
    def test_full_task_spec(self):
        """Test creating task spec with all fields."""
        spec = TaskSpec(
            type=TaskType.CODE,
            payload={"code": "print('hello')", "language": "python"},
            priority=Priority.HIGH,
            orchestra_id="orch-123",
            orchestra_phase="phase-1",
            max_retries=5,
            timeout_seconds=7200,
            min_confidence=0.8,
            preferred_model=ModelTier.OPUS,
            cost_limit_usd=Decimal("10.50"),
            created_by="test-user",
            tags=["urgent", "test"]
        )
        
        assert spec.type == TaskType.CODE
        assert spec.priority == Priority.HIGH
        assert spec.orchestra_id == "orch-123"
        assert spec.orchestra_phase == "phase-1"
        assert spec.max_retries == 5
        assert spec.timeout_seconds == 7200
        assert spec.min_confidence == 0.8
        assert spec.preferred_model == ModelTier.OPUS
        assert spec.cost_limit_usd == Decimal("10.50")
        assert spec.created_by == "test-user"
        assert spec.tags == ["urgent", "test"]
    
    def test_task_spec_validation(self):
        """Test TaskSpec field validation."""
        # Test invalid confidence range
        with pytest.raises(ValidationError):
            TaskSpec(
                type=TaskType.CONTENT,
                payload={},
                min_confidence=1.5  # > 1.0
            )
        
        with pytest.raises(ValidationError):
            TaskSpec(
                type=TaskType.CONTENT,
                payload={},
                min_confidence=-0.1  # < 0.0
            )
    
    def test_task_spec_serialization(self):
        """Test TaskSpec JSON serialization."""
        spec = TaskSpec(
            type=TaskType.RESEARCH,
            payload={"query": "AI trends 2024"},
            priority=Priority.HIGH,
            tags=["research", "trends"]
        )
        
        # Should serialize to JSON without error
        json_data = spec.model_dump_json()
        parsed_data = json.loads(json_data)
        
        assert parsed_data["type"] == "research"
        assert parsed_data["priority"] == 2
        assert parsed_data["payload"]["query"] == "AI trends 2024"
        assert parsed_data["tags"] == ["research", "trends"]


class TestTaskResult:
    """Test TaskResult output schema."""
    
    def test_minimal_task_result(self):
        """Test creating task result with minimal fields."""
        result = TaskResult(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            state=TaskState.SUCCESS,
            confidence=0.85,
            result={"content": "Generated content", "word_count": 500}
        )
        
        assert result.task_id == "task-123"
        assert result.task_type == TaskType.CONTENT
        assert result.state == TaskState.SUCCESS
        assert result.confidence == 0.85
        assert result.confidence_level == ConfidenceLevel.VERY_HIGH  # Auto-calculated (0.85 > 0.8)
        assert result.result["content"] == "Generated content"
        assert result.tokens_consumed == 0  # Default
        assert result.execution_time_seconds == 0.0  # Default
    
    def test_confidence_level_calculation(self):
        """Test automatic confidence level calculation."""
        test_cases = [
            (0.1, ConfidenceLevel.VERY_LOW),
            (0.2, ConfidenceLevel.VERY_LOW),
            (0.3, ConfidenceLevel.LOW),
            (0.4, ConfidenceLevel.LOW),
            (0.5, ConfidenceLevel.MEDIUM),
            (0.6, ConfidenceLevel.MEDIUM),
            (0.7, ConfidenceLevel.HIGH),
            (0.8, ConfidenceLevel.HIGH),
            (0.9, ConfidenceLevel.VERY_HIGH),
            (1.0, ConfidenceLevel.VERY_HIGH)
        ]
        
        for confidence, expected_level in test_cases:
            result = TaskResult(
                task_id="test",
                task_type=TaskType.CONTENT,
                state=TaskState.SUCCESS,
                confidence=confidence,
                result={}
            )
            assert result.confidence_level == expected_level
    
    def test_task_result_with_errors(self):
        """Test task result with errors and warnings."""
        error = TaskError(
            code="VALIDATION_ERROR",
            message="Content too short",
            severity="warning",
            context={"min_length": 100, "actual_length": 50},
            suggestion="Add more content to meet minimum length"
        )
        
        result = TaskResult(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            state=TaskState.FAILED,
            confidence=0.3,
            result={},
            errors=[error],
            warnings=["Low confidence score"]
        )
        
        assert len(result.errors) == 1
        assert result.errors[0].code == "VALIDATION_ERROR"
        assert result.errors[0].severity == "warning"
        assert result.warnings == ["Low confidence score"]
    
    def test_task_result_validation(self):
        """Test TaskResult field validation."""
        # Test invalid confidence range
        with pytest.raises(ValidationError):
            TaskResult(
                task_id="test",
                task_type=TaskType.CONTENT,
                state=TaskState.SUCCESS,
                confidence=2.0,  # > 1.0
                result={}
            )


class TestTaskStatus:
    """Test TaskStatus schema."""
    
    def test_task_status_creation(self):
        """Test creating TaskStatus object."""
        now = datetime.now(timezone.utc)
        
        status = TaskStatus(
            task_id="task-123",
            task_type=TaskType.CODE,
            state=TaskState.RUNNING,
            priority=Priority.HIGH,
            created_at=now,
            retry_count=1,
            max_retries=3
        )
        
        assert status.task_id == "task-123"
        assert status.task_type == TaskType.CODE
        assert status.state == TaskState.RUNNING
        assert status.priority == Priority.HIGH
        assert status.created_at == now
        assert status.retry_count == 1
        assert status.max_retries == 3
    
    def test_task_status_with_orchestra(self):
        """Test TaskStatus with orchestra information."""
        status = TaskStatus(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            state=TaskState.QUEUED,
            priority=Priority.NORMAL,
            created_at=datetime.now(timezone.utc),
            orchestra_id="orch-456",
            orchestra_phase="write"
        )
        
        assert status.orchestra_id == "orch-456"
        assert status.orchestra_phase == "write"


class TestOrchestraSpec:
    """Test OrchestraSpec schema."""
    
    def test_orchestra_spec_creation(self):
        """Test creating OrchestraSpec."""
        spec = OrchestraSpec(
            template="content-pipeline",
            name="Blog Post Creation",
            config={
                "topic": "AI orchestration",
                "word_count": 2000,
                "target_audience": "developers"
            },
            priority=Priority.HIGH,
            cost_budget_usd=Decimal("50.00"),
            time_budget_hours=8,
            created_by="editor-1",
            tags=["blog", "ai", "content"]
        )
        
        assert spec.template == "content-pipeline"
        assert spec.name == "Blog Post Creation"
        assert spec.config["topic"] == "AI orchestration"
        assert spec.priority == Priority.HIGH
        assert spec.cost_budget_usd == Decimal("50.00")
        assert spec.time_budget_hours == 8
        assert spec.created_by == "editor-1"
        assert spec.tags == ["blog", "ai", "content"]


class TestOrchestraStatus:
    """Test OrchestraStatus schema."""
    
    def test_orchestra_status_creation(self):
        """Test creating OrchestraStatus."""
        now = datetime.now(timezone.utc)
        
        status = OrchestraStatus(
            orchestra_id="orch-123",
            template="content-pipeline",
            name="Test Orchestra",
            state=OrchestraState.RUNNING,
            priority=Priority.HIGH,
            created_at=now,
            total_tasks=10,
            completed_tasks=6,
            failed_tasks=1,
            cancelled_tasks=0,
            cost_budget_usd=Decimal("100.00"),
            cost_spent_usd=Decimal("45.50"),
            current_phase="review"
        )
        
        assert status.orchestra_id == "orch-123"
        assert status.template == "content-pipeline"
        assert status.state == OrchestraState.RUNNING
        assert status.total_tasks == 10
        assert status.completed_tasks == 6
        assert status.cost_spent_usd == Decimal("45.50")
        assert status.current_phase == "review"
    
    def test_progress_percentage_calculation(self):
        """Test progress percentage calculation."""
        status = OrchestraStatus(
            orchestra_id="test",
            template="test",
            state=OrchestraState.RUNNING,
            priority=Priority.NORMAL,
            created_at=datetime.now(timezone.utc),
            total_tasks=10,
            completed_tasks=7
        )
        
        assert status.progress_percentage == 70.0
        
        # Test zero division
        status.total_tasks = 0
        assert status.progress_percentage == 0.0


class TestQueueStats:
    """Test QueueStats schema."""
    
    def test_queue_stats_creation(self):
        """Test creating QueueStats."""
        stats = QueueStats(
            queued=15,
            running=3,
            completed=100,
            failed=5,
            retrying=2,
            cancelled=1,
            priority_breakdown={"priority_1": 2, "priority_3": 18},
            type_breakdown={"content": 10, "code": 8, "research": 7},
            avg_execution_time_seconds=45.2,
            active_workers=3,
            max_workers=8,
            dead_letter_count=2
        )
        
        assert stats.queued == 15
        assert stats.running == 3
        assert stats.total_tasks == 126  # Sum of all states
        assert stats.worker_utilization == 37.5  # 3/8 * 100
        assert stats.priority_breakdown["priority_1"] == 2
        assert stats.type_breakdown["content"] == 10
        assert stats.dead_letter_count == 2
    
    def test_worker_utilization_calculation(self):
        """Test worker utilization calculation."""
        stats = QueueStats(active_workers=5, max_workers=8)
        assert stats.worker_utilization == 62.5
        
        # Test zero max workers
        stats = QueueStats(active_workers=0, max_workers=0)
        assert stats.worker_utilization == 0.0


class TestTaskFilters:
    """Test TaskFilters schema."""
    
    def test_task_filters_creation(self):
        """Test creating TaskFilters."""
        filters = TaskFilters(
            states=[TaskState.QUEUED, TaskState.RUNNING],
            types=[TaskType.CONTENT, TaskType.CODE],
            priorities=[Priority.HIGH, Priority.CRITICAL],
            orchestra_id="orch-123",
            limit=50,
            offset=10
        )
        
        assert len(filters.states) == 2
        assert TaskState.QUEUED in filters.states
        assert TaskState.RUNNING in filters.states
        assert len(filters.types) == 2
        assert len(filters.priorities) == 2
        assert filters.orchestra_id == "orch-123"
        assert filters.limit == 50
        assert filters.offset == 10
    
    def test_task_filters_validation(self):
        """Test TaskFilters validation."""
        # Test invalid limit
        with pytest.raises(ValidationError):
            TaskFilters(limit=0)  # Must be >= 1
        
        with pytest.raises(ValidationError):
            TaskFilters(limit=2000)  # Must be <= 1000
        
        # Test invalid offset
        with pytest.raises(ValidationError):
            TaskFilters(offset=-1)  # Must be >= 0


class TestUtilityFunctions:
    """Test utility functions."""
    
    def test_generate_task_id(self):
        """Test task ID generation."""
        task_id = generate_task_id()
        
        assert isinstance(task_id, str)
        assert len(task_id) == 36  # UUID4 string length
        assert task_id.count('-') == 4  # UUID format
        
        # Test uniqueness
        task_id2 = generate_task_id()
        assert task_id != task_id2
    
    def test_generate_orchestra_id(self):
        """Test orchestra ID generation."""
        orchestra_id = generate_orchestra_id()
        
        assert isinstance(orchestra_id, str)
        assert len(orchestra_id) == 36
        
        # Test uniqueness
        orchestra_id2 = generate_orchestra_id()
        assert orchestra_id != orchestra_id2
    
    def test_calculate_retry_delay(self):
        """Test retry delay calculation with exponential backoff."""
        # Test exponential backoff: 1, 2, 4, 8, 16, 32, 60 (capped)
        expected_delays = [1, 2, 4, 8, 16, 32, 60, 60, 60]
        
        for attempt, expected in enumerate(expected_delays, 1):
            delay = calculate_retry_delay(attempt)
            assert delay == expected
    
    def test_select_model_tier(self):
        """Test model tier selection with escalation."""
        # Test content task escalation path
        assert select_model_tier(TaskType.CONTENT, 1) == ModelTier.HAIKU
        assert select_model_tier(TaskType.CONTENT, 2) == ModelTier.SONNET
        assert select_model_tier(TaskType.CONTENT, 3) == ModelTier.OPUS
        assert select_model_tier(TaskType.CONTENT, 4) == ModelTier.OPUS  # Capped
        
        # Test code task escalation path (starts higher)
        assert select_model_tier(TaskType.CODE, 1) == ModelTier.SONNET
        assert select_model_tier(TaskType.CODE, 2) == ModelTier.OPUS
        assert select_model_tier(TaskType.CODE, 3) == ModelTier.OPUS
        
        # Test translation task escalation (quality critical)
        assert select_model_tier(TaskType.TRANSLATION, 1) == ModelTier.SONNET
        assert select_model_tier(TaskType.TRANSLATION, 2) == ModelTier.OPUS
    
    def test_default_max_retries(self):
        """Test default max retries configuration."""
        assert DEFAULT_MAX_RETRIES[TaskType.CONTENT] == 3
        assert DEFAULT_MAX_RETRIES[TaskType.CODE] == 2
        assert DEFAULT_MAX_RETRIES[TaskType.RESEARCH] == 3
        assert DEFAULT_MAX_RETRIES[TaskType.TRANSLATION] == 4
        assert DEFAULT_MAX_RETRIES[TaskType.REVIEW] == 2


class TestSerialization:
    """Test JSON serialization/deserialization of schemas."""
    
    def test_task_spec_roundtrip(self):
        """Test TaskSpec JSON roundtrip."""
        spec = TaskSpec(
            type=TaskType.RESEARCH,
            payload={"query": "AI trends", "sources": 10},
            priority=Priority.HIGH,
            tags=["research", "ai"],
            cost_limit_usd=Decimal("25.50")
        )
        
        # Serialize to JSON
        json_data = spec.model_dump_json()

        # Deserialize from JSON
        parsed_spec = TaskSpec.model_validate_json(json_data)
        
        assert parsed_spec.type == spec.type
        assert parsed_spec.payload == spec.payload
        assert parsed_spec.priority == spec.priority
        assert parsed_spec.tags == spec.tags
        assert parsed_spec.cost_limit_usd == spec.cost_limit_usd
    
    def test_task_result_roundtrip(self):
        """Test TaskResult JSON roundtrip."""
        now = datetime.now(timezone.utc)
        
        result = TaskResult(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            state=TaskState.SUCCESS,
            confidence=0.85,
            result={"content": "Generated text", "word_count": 500},
            created_at=now,
            model_used="sonnet-4",
            tokens_consumed=1500,
            execution_time_seconds=45.2
        )
        
        # Serialize to JSON
        json_data = result.model_dump_json()

        # Deserialize from JSON
        parsed_result = TaskResult.model_validate_json(json_data)
        
        assert parsed_result.task_id == result.task_id
        assert parsed_result.task_type == result.task_type
        assert parsed_result.state == result.state
        assert parsed_result.confidence == result.confidence
        assert parsed_result.result == result.result
        assert parsed_result.model_used == result.model_used
        assert parsed_result.tokens_consumed == result.tokens_consumed