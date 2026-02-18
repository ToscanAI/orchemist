"""Tests for the task runner system."""

import pytest
import threading
import time
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

from src.orchestration_engine.runner import (
    TaskRunner, DryRunExecutor, LocalExecutor, OpenClawExecutor,
    TaskExecutor
)
from src.orchestration_engine.config import EngineConfig, QueueConfig, ModelsConfig
from src.orchestration_engine.schemas import TaskSpec, TaskType, Priority, TaskState
from src.orchestration_engine.db import Database


@pytest.fixture
def test_config():
    """Create test configuration."""
    return EngineConfig(
        queue=QueueConfig(max_workers=2, poll_interval_seconds=1),
        models=ModelsConfig(default_tier="sonnet-4"),
        dry_run=True  # Use dry run mode for testing
    )


@pytest.fixture
def test_db():
    """Create test database."""
    return Database(":memory:")


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
        assert result.errors[0]["code"] == "dry_run_failure"
    
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


class TestLocalExecutor:
    """Test the local executor."""
    
    def test_local_executor_success(self):
        """Test successful local command execution."""
        executor = LocalExecutor(allowed_commands=["echo"])
        
        task = TaskSpec(
            type=TaskType.CODE,
            payload={"command": "echo 'Hello World'"},
            priority=Priority.NORMAL
        )
        
        result = executor.execute(task, "worker-1")
        
        assert result.state == TaskState.SUCCESS
        assert result.confidence == 1.0
        assert "Hello World" in result.result["stdout"]
        assert result.result["return_code"] == 0
    
    def test_local_executor_command_failure(self):
        """Test failed local command execution."""
        executor = LocalExecutor(allowed_commands=["false"])
        
        task = TaskSpec(
            type=TaskType.CODE,
            payload={"command": "false"},  # Command that always fails
            priority=Priority.NORMAL
        )
        
        result = executor.execute(task, "worker-1")
        
        assert result.state == TaskState.FAILED
        assert result.confidence == 0.0
        assert len(result.errors) > 0
        assert "Command failed" in result.errors[0]["message"]
    
    def test_local_executor_security_check(self):
        """Test security check for disallowed commands."""
        executor = LocalExecutor(allowed_commands=["echo"])
        
        task = TaskSpec(
            type=TaskType.CODE,
            payload={"command": "rm -rf /"},  # Dangerous command
            priority=Priority.NORMAL
        )
        
        result = executor.execute(task, "worker-1")
        
        assert result.state == TaskState.FAILED
        assert "not allowed" in result.errors[0]["message"]
    
    def test_local_executor_missing_command(self):
        """Test execution with missing command."""
        executor = LocalExecutor()
        
        task = TaskSpec(
            type=TaskType.CODE,
            payload={"data": "no command field"},
            priority=Priority.NORMAL
        )
        
        result = executor.execute(task, "worker-1")
        
        assert result.state == TaskState.FAILED
        assert "No 'command' specified" in result.errors[0]["message"]
    
    def test_local_executor_timeout(self):
        """Test command timeout handling."""
        executor = LocalExecutor(allowed_commands=["sleep"])
        
        task = TaskSpec(
            type=TaskType.CODE,
            payload={"command": "sleep 10"},  # Long running command
            priority=Priority.NORMAL,
            timeout_seconds=1  # Short timeout
        )
        
        start_time = time.time()
        result = executor.execute(task, "worker-1")
        execution_time = time.time() - start_time
        
        assert result.state == TaskState.FAILED
        assert execution_time < 5  # Should timeout quickly
        assert "timed out" in result.errors[0]["message"]
    
    def test_local_executor_task_type_handling(self):
        """Test which task types local executor can handle."""
        executor = LocalExecutor()
        
        # Should handle code and review tasks
        assert executor.can_handle(TaskType.CODE)
        assert executor.can_handle(TaskType.REVIEW)
        
        # Should not handle content tasks
        assert not executor.can_handle(TaskType.CONTENT)
    
    def test_local_executor_zero_cost(self, sample_task):
        """Test that local execution is free."""
        executor = LocalExecutor()
        
        cost = executor.estimate_cost(sample_task)
        assert cost == 0.0


class TestOpenClawExecutor:
    """Test the OpenClaw executor."""
    
    def test_openclaw_executor_initialization(self, test_config):
        """Test OpenClaw executor initialization."""
        executor = OpenClawExecutor(test_config)
        
        assert executor.config == test_config
    
    def test_openclaw_executor_can_handle_all(self, test_config):
        """Test that OpenClaw executor can handle all task types."""
        executor = OpenClawExecutor(test_config)
        
        for task_type in TaskType:
            assert executor.can_handle(task_type)
    
    def test_prompt_formatting_content(self, test_config):
        """Test prompt formatting for content tasks."""
        executor = OpenClawExecutor(test_config)
        
        task = TaskSpec(
            type=TaskType.CONTENT,
            payload={"topic": "AI in healthcare", "style": "blog post"},
            priority=Priority.NORMAL
        )
        
        prompt = executor._format_task_prompt(task)
        
        assert "Create content" in prompt
        assert "AI in healthcare" in prompt
        assert "blog post" in prompt
    
    def test_prompt_formatting_code(self, test_config):
        """Test prompt formatting for code tasks."""
        executor = OpenClawExecutor(test_config)
        
        task = TaskSpec(
            type=TaskType.CODE,
            payload={"language": "python", "requirements": "Sort a list"},
            priority=Priority.NORMAL
        )
        
        prompt = executor._format_task_prompt(task)
        
        assert "Write code" in prompt
        assert "python" in prompt
        assert "Sort a list" in prompt
    
    def test_prompt_quality_requirements(self, test_config):
        """Test prompt includes quality requirements."""
        executor = OpenClawExecutor(test_config)
        
        task = TaskSpec(
            type=TaskType.RESEARCH,
            payload={"topic": "Climate change"},
            min_confidence=0.9,
            priority=Priority.NORMAL
        )
        
        prompt = executor._format_task_prompt(task)
        
        assert "Minimum confidence level 0.9" in prompt
    
    def test_openclaw_execution_simulation_success(self, test_config, sample_task):
        """Test simulated successful OpenClaw execution."""
        executor = OpenClawExecutor(test_config)
        
        with patch.object(executor, '_simulate_openclaw_execution') as mock_sim:
            mock_sim.return_value = {
                'success': True,
                'confidence': 0.85,
                'output': {'result': 'Test result'},
                'tokens_used': 500,
                'cost_usd': 0.05
            }
            
            result = executor.execute(sample_task, "worker-1", "sonnet-4")
            
            assert result.state == TaskState.SUCCESS
            assert result.confidence == 0.85
            assert result.tokens_consumed == 500
            assert result.cost_usd == 0.05
    
    def test_openclaw_execution_simulation_failure(self, test_config, sample_task):
        """Test simulated failed OpenClaw execution."""
        executor = OpenClawExecutor(test_config)
        
        with patch.object(executor, '_simulate_openclaw_execution') as mock_sim:
            mock_sim.return_value = {
                'success': False,
                'confidence': 0.0,
                'output': {},
                'errors': [{'code': 'test_error', 'message': 'Test failure'}],
                'tokens_used': 100,
                'cost_usd': 0.01
            }
            
            result = executor.execute(sample_task, "worker-1", "sonnet-4")
            
            assert result.state == TaskState.FAILED
            assert result.confidence == 0.0
            assert len(result.errors) > 0
    
    def test_openclaw_cost_estimation(self, test_config):
        """Test OpenClaw cost estimation."""
        executor = OpenClawExecutor(test_config)
        
        task = TaskSpec(
            type=TaskType.CONTENT,
            payload={"text": "A" * 1000},  # Large payload
            priority=Priority.NORMAL
        )
        
        cost = executor.estimate_cost(task)
        
        assert isinstance(cost, float)
        assert cost > 0
        
        # Larger payload should cost more
        small_task = TaskSpec(
            type=TaskType.CONTENT,
            payload={"text": "Small"},
            priority=Priority.NORMAL
        )
        
        small_cost = executor.estimate_cost(small_task)
        assert cost > small_cost


class TestTaskRunner:
    """Test the main task runner."""
    
    def test_task_runner_initialization(self, test_db, test_config):
        """Test task runner initialization."""
        runner = TaskRunner(test_db, test_config)
        
        assert runner.config == test_config
        assert runner.db == test_db
        assert runner.queue is not None
        assert runner.worker_pool is not None
        assert runner.recovery_manager is not None
        assert runner.progress_tracker is not None
        assert len(runner.executors) > 0
        assert not runner._running
    
    def test_executor_initialization_dry_run(self, test_db):
        """Test executor initialization in dry run mode."""
        config = EngineConfig(dry_run=True)
        runner = TaskRunner(test_db, config)
        
        # Should have DryRunExecutor
        assert any(isinstance(ex, DryRunExecutor) for ex in runner.executors)
    
    def test_executor_initialization_production(self, test_db):
        """Test executor initialization in production mode."""
        config = EngineConfig(dry_run=False)
        runner = TaskRunner(test_db, config)
        
        # Should have LocalExecutor and OpenClawExecutor
        assert any(isinstance(ex, LocalExecutor) for ex in runner.executors)
        assert any(isinstance(ex, OpenClawExecutor) for ex in runner.executors)
    
    def test_task_runner_start_stop(self, test_db, test_config):
        """Test task runner start and stop."""
        runner = TaskRunner(test_db, test_config)
        
        # Start runner
        runner.start()
        assert runner._running
        assert runner._runner_thread is not None
        
        # Stop runner
        runner.stop()
        assert not runner._running
    
    def test_executor_selection(self, test_db, test_config):
        """Test executor selection for different task types."""
        runner = TaskRunner(test_db, test_config)
        
        # Should find executor for each task type
        for task_type in TaskType:
            executor = runner._select_executor(task_type)
            assert executor is not None
            assert executor.can_handle(task_type)
    
    def test_model_tier_selection(self, test_db, test_config):
        """Test model tier selection."""
        runner = TaskRunner(test_db, test_config)
        
        # Test default selection
        task = TaskSpec(type=TaskType.CONTENT, payload={})
        model_tier = runner._select_model_tier(task, is_retry=False)
        assert model_tier == test_config.models.default_tier
        
        # Test preferred model
        preferred_task = TaskSpec(
            type=TaskType.CONTENT, 
            payload={}, 
            preferred_model="opus-4-6"
        )
        model_tier = runner._select_model_tier(preferred_task, is_retry=False)
        assert model_tier == "opus-4-6"
    
    def test_immediate_task_execution(self, test_db, test_config):
        """Test immediate task execution."""
        runner = TaskRunner(test_db, test_config)
        
        # Mock the queue to return a task
        mock_task = TaskSpec(
            type=TaskType.CONTENT,
            payload={"test": "data"},
            priority=Priority.HIGH
        )
        mock_task.id = "test-task-123"
        
        with patch.object(runner.queue, 'get_task', return_value=mock_task):
            with patch.object(runner, '_execute_task') as mock_execute:
                success = runner.execute_task_immediately("test-task-123")
                
                assert success
                mock_execute.assert_called_once_with(mock_task)
    
    def test_immediate_execution_no_capacity(self, test_db, test_config):
        """Test immediate execution when no worker capacity."""
        runner = TaskRunner(test_db, test_config)
        
        # Mock no available capacity
        with patch.object(runner.worker_pool, 'get_available_capacity', return_value=0):
            success = runner.execute_task_immediately("test-task")
            assert not success
    
    def test_runner_status(self, test_db, test_config):
        """Test runner status reporting."""
        runner = TaskRunner(test_db, test_config)
        
        status = runner.get_status()
        
        assert isinstance(status, dict)
        assert "running" in status
        assert "worker_pool" in status
        assert "queue_stats" in status
        assert "recovery_stats" in status
        assert "active_tasks" in status
        assert "executors" in status
        
        # Check executor info
        assert len(status["executors"]) > 0
        for executor_info in status["executors"]:
            assert "type" in executor_info
            assert "can_handle" in executor_info


class TestTaskExecutionWorkflow:
    """Test complete task execution workflows."""
    
    def test_successful_task_workflow(self, test_db, test_config):
        """Test complete successful task execution workflow."""
        runner = TaskRunner(test_db, test_config)
        
        # Create a test task
        task = TaskSpec(
            type=TaskType.CONTENT,
            payload={"topic": "test content"},
            priority=Priority.NORMAL
        )
        task.id = "workflow-test-123"
        
        # Mock successful execution
        with patch.object(runner.worker_pool, 'assign_task', return_value="worker-1"):
            with patch.object(runner.worker_pool, 'start_task_execution'):
                with patch.object(runner.worker_pool, 'complete_task'):
                    with patch.object(runner.queue, 'complete_task'):
                        with patch.object(runner.progress_tracker, 'task_started'):
                            with patch.object(runner.progress_tracker, 'task_completed'):
                                
                                # Mock executor to return success
                                mock_executor = Mock()
                                mock_result = Mock()
                                mock_result.state = TaskState.SUCCESS
                                mock_result.confidence = 0.9
                                mock_result.tokens_consumed = 100
                                mock_result.cost_usd = 0.05
                                mock_executor.execute.return_value = mock_result
                                
                                with patch.object(runner, '_select_executor', return_value=mock_executor):
                                    runner._execute_task_in_worker(task, "worker-1", False)
                                
                                # Verify success handling was called
                                runner.queue.complete_task.assert_called_once()
                                runner.progress_tracker.task_completed.assert_called_once()
    
    def test_failed_task_workflow(self, test_db, test_config):
        """Test failed task execution workflow."""
        runner = TaskRunner(test_db, test_config)
        
        task = TaskSpec(
            type=TaskType.CODE,
            payload={"command": "invalid"},
            priority=Priority.NORMAL
        )
        task.id = "failed-test-123"
        
        # Mock failed execution
        with patch.object(runner.worker_pool, 'assign_task', return_value="worker-1"):
            with patch.object(runner.worker_pool, 'start_task_execution'):
                with patch.object(runner.worker_pool, 'complete_task'):
                    with patch.object(runner.recovery_manager, 'handle_task_failure') as mock_recovery:
                        mock_recovery.return_value = (False, None, None)  # No retry
                        
                        with patch.object(runner.queue, 'fail_task'):
                            with patch.object(runner.progress_tracker, 'task_failed'):
                                
                                # Mock executor to return failure
                                mock_executor = Mock()
                                mock_result = Mock()
                                mock_result.state = TaskState.FAILED
                                mock_result.errors = [{"message": "Test failure"}]
                                mock_executor.execute.return_value = mock_result
                                
                                with patch.object(runner, '_select_executor', return_value=mock_executor):
                                    runner._execute_task_in_worker(task, "worker-1", False)
                                
                                # Verify failure handling
                                mock_recovery.assert_called_once()
                                runner.queue.fail_task.assert_called_once()
    
    def test_task_retry_workflow(self, test_db, test_config):
        """Test task retry workflow."""
        runner = TaskRunner(test_db, test_config)
        
        task = TaskSpec(
            type=TaskType.RESEARCH,
            payload={"query": "test research"},
            priority=Priority.NORMAL
        )
        task.id = "retry-test-123"
        
        retry_time = datetime.now()
        
        # Mock failed execution with retry
        with patch.object(runner.worker_pool, 'assign_task', return_value="worker-1"):
            with patch.object(runner.worker_pool, 'start_task_execution'):
                with patch.object(runner.worker_pool, 'complete_task'):
                    with patch.object(runner.recovery_manager, 'handle_task_failure') as mock_recovery:
                        mock_recovery.return_value = (True, retry_time, "opus-4-6")  # Retry with escalation
                        
                        with patch.object(runner.queue, 'schedule_retry') as mock_schedule:
                            with patch.object(runner.progress_tracker, 'task_retry_scheduled'):
                                
                                # Mock executor failure
                                mock_executor = Mock()
                                mock_result = Mock()
                                mock_result.state = TaskState.FAILED
                                mock_result.errors = [{"message": "Transient error"}]
                                mock_executor.execute.return_value = mock_result
                                
                                with patch.object(runner, '_select_executor', return_value=mock_executor):
                                    runner._execute_task_in_worker(task, "worker-1", False)
                                
                                # Verify retry scheduling
                                mock_schedule.assert_called_once_with("retry-test-123", retry_time, "opus-4-6")


class TestTaskRunnerConcurrency:
    """Test task runner concurrency and thread safety."""
    
    def test_concurrent_task_execution(self, test_db, test_config):
        """Test concurrent task execution."""
        runner = TaskRunner(test_db, test_config)
        
        results = []
        
        def execute_task(task_id):
            task = TaskSpec(
                type=TaskType.CONTENT,
                payload={"id": task_id},
                priority=Priority.NORMAL
            )
            task.id = task_id
            
            # Mock successful workflow
            with patch.object(runner.worker_pool, 'assign_task', return_value=f"worker-{task_id}"):
                with patch.object(runner.worker_pool, 'start_task_execution'):
                    with patch.object(runner.worker_pool, 'complete_task'):
                        mock_executor = Mock()
                        mock_result = Mock()
                        mock_result.state = TaskState.SUCCESS
                        mock_executor.execute.return_value = mock_result
                        
                        with patch.object(runner, '_select_executor', return_value=mock_executor):
                            runner._execute_task_in_worker(task, f"worker-{task_id}", False)
                            results.append(task_id)
        
        # Execute tasks concurrently
        threads = []
        for i in range(3):
            thread = threading.Thread(target=execute_task, args=(f"task-{i}",))
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join()
        
        # All tasks should complete
        assert len(results) == 3
        assert "task-0" in results
        assert "task-1" in results
        assert "task-2" in results
    
    def test_runner_thread_lifecycle(self, test_db, test_config):
        """Test runner thread lifecycle."""
        runner = TaskRunner(test_db, test_config)
        
        # Start runner
        runner.start()
        
        # Runner thread should be alive
        assert runner._runner_thread.is_alive()
        
        # Let it run briefly
        time.sleep(0.5)
        
        # Stop runner
        runner.stop()
        
        # Thread should terminate
        runner._runner_thread.join(timeout=2)
        assert not runner._runner_thread.is_alive()


class TestTaskRunnerIntegration:
    """Integration tests for task runner with all components."""
    
    def test_end_to_end_task_processing(self, test_db, test_config):
        """Test end-to-end task processing."""
        runner = TaskRunner(test_db, test_config)
        
        # Submit a task to the queue
        task_spec = TaskSpec(
            type=TaskType.CONTENT,
            payload={"topic": "Integration test"},
            priority=Priority.HIGH
        )
        
        task_id = runner.queue.submit_task(task_spec)
        
        # Start runner briefly
        runner.start()
        
        try:
            # Let it process
            time.sleep(1)
            
            # Check task status
            status = runner.queue.get_task_status(task_id)
            
            # Task should have been processed (success or failure)
            assert status.state in [TaskState.SUCCESS, TaskState.FAILED, TaskState.RUNNING]
        
        finally:
            runner.stop()
    
    def test_runner_with_real_database(self, test_config):
        """Test runner with real database operations."""
        # Use in-memory database for testing
        db = Database(":memory:")
        runner = TaskRunner(db, test_config)
        
        # Verify database tables are created
        tables = db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = [table['name'] for table in tables]
        
        # Should have all required tables
        expected_tables = ['tasks', 'workers', 'progress_events', 'retry_attempts']
        for expected in expected_tables:
            assert expected in table_names
        
        # Test basic operations work
        task_spec = TaskSpec(
            type=TaskType.REVIEW,
            payload={"content": "Test review"},
            priority=Priority.NORMAL
        )
        
        task_id = runner.queue.submit_task(task_spec)
        assert task_id is not None
        
        # Verify task in database
        task_status = runner.queue.get_task_status(task_id)
        assert task_status.state == TaskState.QUEUED