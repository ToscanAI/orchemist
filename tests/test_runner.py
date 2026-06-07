"""Tests for the DryRunExecutor."""

import pytest

from src.orchestration_engine.runner import DryRunExecutor
from src.orchestration_engine.schemas import TaskSpec, TaskType, Priority, TaskState


@pytest.fixture
def sample_task():
    """Create sample task for testing."""
    return TaskSpec(
        type=TaskType.CONTENT,
        payload={"text": "Create a blog post about AI"},
        priority=Priority.NORMAL,
        timeout_seconds=300
    )


class TestDryRunExecutor:
    """Test the dry run executor."""
    
    def test_dry_run_executor_success(self, sample_task):
        """Test successful dry run execution."""
        executor = DryRunExecutor(delay_seconds=0.1, failure_rate=0.0)
        
        result = executor.execute(sample_task, "worker-1", "sonnet-4")
        
        assert result.state == TaskState.SUCCESS
        assert result.confidence > 0.7
        assert result.task_type == TaskType.CONTENT
        assert result.model_used == "sonnet-4"
        assert result.tokens_consumed > 0
        assert result.execution_time_seconds > 0
    
    def test_dry_run_executor_failure(self, sample_task):
        """Test dry run execution failure."""
        executor = DryRunExecutor(delay_seconds=0.1, failure_rate=1.0)  # Always fail
        
        result = executor.execute(sample_task, "worker-1", "sonnet-4")
        
        assert result.state == TaskState.FAILED
        assert result.confidence == 0.0
        assert len(result.errors) > 0
        assert result.errors[0].code == "dry_run_failure"
    
    def test_dry_run_executor_can_handle_all(self):
        """Test that dry run executor can handle all task types."""
        executor = DryRunExecutor()
        
        for task_type in TaskType:
            assert executor.can_handle(task_type)
    
    def test_dry_run_executor_cost_estimate(self, sample_task):
        """Test cost estimation."""
        executor = DryRunExecutor()
        
        cost = executor.estimate_cost(sample_task)
        assert isinstance(cost, float)
        assert cost > 0

