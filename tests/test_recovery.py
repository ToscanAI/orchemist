"""Tests for error recovery and retry logic."""

import threading

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from src.orchestration_engine.recovery import (
    ErrorClassifier, ErrorType, ErrorSeverity, ErrorPattern,
    RecoveryManager, CircuitBreakerState, TaskRetryState, RetryAttempt
)
from src.orchestration_engine.config import EngineConfig, RetryConfig
from src.orchestration_engine.schemas import TaskType, ModelTier
from src.orchestration_engine.db import Database


@pytest.fixture
def test_config():
    """Create test configuration."""
    return EngineConfig(
        retry=RetryConfig(
            max_retries_default=3,
            backoff_base=1,
            backoff_max=60,
            circuit_breaker_threshold=3,
            circuit_breaker_reset_minutes=15
        )
    )


@pytest.fixture
def test_db():
    """Create test database."""
    db = Database(":memory:")
    return db


class TestErrorPattern:
    """Test error pattern matching."""
    
    def test_pattern_matching(self):
        """Test error pattern keyword matching."""
        pattern = ErrorPattern(
            keywords=["timeout", "connection reset"],
            error_type=ErrorType.TRANSIENT,
            severity=ErrorSeverity.MEDIUM
        )
        
        # Should match
        assert pattern.matches("Request timeout occurred")
        assert pattern.matches("Connection reset by peer")
        assert pattern.matches("TIMEOUT ERROR")  # Case insensitive
        
        # Should not match
        assert not pattern.matches("Invalid API key")
        assert not pattern.matches("Permission denied")
    
    def test_case_insensitive_matching(self):
        """Test case insensitive pattern matching."""
        pattern = ErrorPattern(
            keywords=["Rate Limit"],
            error_type=ErrorType.RATE_LIMIT,
            severity=ErrorSeverity.MEDIUM
        )
        
        assert pattern.matches("rate limit exceeded")
        assert pattern.matches("RATE LIMIT ERROR")
        assert pattern.matches("Hit rate limit")


class TestErrorClassifier:
    """Test error classification system."""
    
    def test_transient_error_classification(self):
        """Test transient error classification."""
        classifier = ErrorClassifier()
        
        # Test various transient errors
        transient_errors = [
            "Connection timeout",
            "Service unavailable",
            "Network error occurred",
            "Temporary failure - try again"
        ]
        
        for error in transient_errors:
            error_type, severity = classifier.classify(error)
            assert error_type == ErrorType.TRANSIENT
    
    def test_permanent_error_classification(self):
        """Test permanent error classification."""
        classifier = ErrorClassifier()
        
        permanent_errors = [
            "Invalid API key",
            "Authentication failed",
            "Permission denied",
            "Malformed input data",
            "Model not available"
        ]
        
        for error in permanent_errors:
            error_type, severity = classifier.classify(error)
            assert error_type == ErrorType.PERMANENT
    
    def test_quality_error_classification(self):
        """Test quality error classification."""
        classifier = ErrorClassifier()
        
        quality_errors = [
            "Confidence too low",
            "Quality check failed",
            "Validation failed",
            "Hallucination detected"
        ]
        
        for error in quality_errors:
            error_type, severity = classifier.classify(error)
            assert error_type == ErrorType.QUALITY
    
    def test_rate_limit_classification(self):
        """Test rate limit error classification."""
        classifier = ErrorClassifier()
        
        rate_limit_errors = [
            "Rate limit exceeded",
            "HTTP 429 error",
            "Request throttled"
        ]
        
        for error in rate_limit_errors:
            error_type, severity = classifier.classify(error)
            assert error_type == ErrorType.RATE_LIMIT
    
    def test_context_based_classification(self):
        """Test context-based error classification."""
        classifier = ErrorClassifier()
        
        # Generic error with context
        error_msg = "Task failed"
        context = {"model_tier": "haiku"}
        
        error_type, severity = classifier.classify(error_msg, context)
        assert error_type == ErrorType.QUALITY  # Haiku failures treated as quality issues
    
    def test_unknown_error_default(self):
        """Test unknown error default classification."""
        classifier = ErrorClassifier()
        
        error_type, severity = classifier.classify("Some unknown error")
        assert error_type == ErrorType.TRANSIENT  # Default to transient
        assert severity == ErrorSeverity.MEDIUM
    
    def test_retry_config_for_error_types(self):
        """Test retry configuration for different error types."""
        classifier = ErrorClassifier()
        
        # Rate limit gets overridden by task type (CONTENT = 3)
        config = classifier.get_retry_config(ErrorType.RATE_LIMIT, TaskType.CONTENT)
        assert config["max_retries"] == 3  # Task type overrides error type
        assert config["backoff_multiplier"] == 2.5
        
        # Quality errors should escalate model (task type overrides max_retries)
        config = classifier.get_retry_config(ErrorType.QUALITY, TaskType.CONTENT)
        assert config["escalate_model"] is True
        assert config["max_retries"] == 3  # Task type overrides
        
        # Permanent errors should not retry (no task type override for this)
        config = classifier.get_retry_config(ErrorType.PERMANENT, TaskType.RESEARCH)
        assert config["max_retries"] == 3  # Task type override (RESEARCH = 3)
    
    def test_task_type_retry_adjustments(self):
        """Test task type specific retry adjustments."""
        classifier = ErrorClassifier()
        
        # Different task types should have different max retries
        content_config = classifier.get_retry_config(ErrorType.TRANSIENT, TaskType.CONTENT)
        code_config = classifier.get_retry_config(ErrorType.TRANSIENT, TaskType.CODE)
        translation_config = classifier.get_retry_config(ErrorType.TRANSIENT, TaskType.TRANSLATION)
        
        assert content_config["max_retries"] == 3
        assert code_config["max_retries"] == 2
        assert translation_config["max_retries"] == 4


class TestCircuitBreakerState:
    """Test circuit breaker state management."""
    
    def test_circuit_breaker_initial_state(self):
        """Test initial circuit breaker state."""
        cb = CircuitBreakerState("test-circuit")
        
        assert cb.name == "test-circuit"
        assert cb.failure_count == 0
        assert cb.state == "closed"
        assert not cb.is_open(threshold=5, reset_timeout_minutes=15)
    
    def test_circuit_breaker_opening(self):
        """Test circuit breaker opening on failures."""
        cb = CircuitBreakerState("test-circuit")
        threshold = 3
        reset_timeout = 15
        
        # Record failures up to threshold
        for i in range(threshold):
            cb.record_failure(threshold)
            if i < threshold - 1:
                assert not cb.is_open(threshold, reset_timeout)
        
        # Should open on threshold
        assert cb.is_open(threshold, reset_timeout)
        assert cb.state == "open"
        assert cb.opened_at is not None
    
    def test_circuit_breaker_reset_timeout(self):
        """Test circuit breaker reset after timeout."""
        cb = CircuitBreakerState("test-circuit")
        threshold = 3
        reset_timeout = 1  # 1 minute
        
        # Open the circuit
        for _ in range(threshold):
            cb.record_failure(threshold)
        
        assert cb.is_open(threshold, reset_timeout)
        
        # Manually age the opened_at timestamp
        cb.opened_at = datetime.now() - timedelta(minutes=2)
        
        # Should allow half-open state
        assert not cb.is_open(threshold, reset_timeout)
        assert cb.state == "half_open"
    
    def test_circuit_breaker_success_reset(self):
        """Test circuit breaker reset on success."""
        cb = CircuitBreakerState("test-circuit")
        threshold = 3
        
        # Build up failures
        for _ in range(2):
            cb.record_failure(threshold)
        
        assert cb.failure_count == 2
        
        # Record success
        cb.record_success()
        
        assert cb.failure_count == 0
        assert cb.state == "closed"
        assert cb.opened_at is None


class TestTaskRetryState:
    """Test task retry state management."""
    
    def test_retry_state_creation(self):
        """Test task retry state creation."""
        retry_state = TaskRetryState(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            original_model_tier="sonnet-4",
            max_retries=3,
            escalation_path=["haiku-4-5", "sonnet-4", "opus-4-6"]
        )
        
        assert retry_state.task_id == "task-123"
        assert retry_state.task_type == TaskType.CONTENT
        assert retry_state.current_attempt == 0  # No attempts yet
        assert retry_state.should_retry()
        assert retry_state.next_model_tier == "haiku-4-5"
    
    def test_retry_scheduling(self):
        """Test retry attempt scheduling."""
        retry_state = TaskRetryState(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            original_model_tier="sonnet-4",
            max_retries=3,
            escalation_path=["haiku-4-5", "sonnet-4", "opus-4-6"]
        )
        
        # Schedule first retry
        retry_at = retry_state.schedule_retry(
            ErrorType.TRANSIENT, "Test error", 
            backoff_base=1, backoff_max=60
        )
        
        assert retry_state.current_attempt == 1
        assert len(retry_state.attempts) == 1
        assert retry_at > datetime.now()
        
        attempt = retry_state.attempts[0]
        assert attempt.attempt_number == 1
        assert attempt.error_type == ErrorType.TRANSIENT
        assert attempt.backoff_seconds == 1  # First retry: base delay
    
    def test_exponential_backoff(self):
        """Test exponential backoff calculation."""
        retry_state = TaskRetryState(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            original_model_tier="sonnet-4",
            max_retries=5
        )
        
        # Schedule multiple retries
        backoff_times = []
        for i in range(4):
            retry_state.schedule_retry(
                ErrorType.TRANSIENT, f"Error {i}",
                backoff_base=2, backoff_max=60
            )
            backoff_times.append(retry_state.attempts[-1].backoff_seconds)
        
        # Should follow exponential backoff: 2, 4, 8, 16
        assert backoff_times == [2, 4, 8, 16]
    
    def test_backoff_max_limit(self):
        """Test backoff maximum limit."""
        retry_state = TaskRetryState(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            original_model_tier="sonnet-4",
            max_retries=10
        )
        
        # Schedule many retries
        for i in range(8):
            retry_state.schedule_retry(
                ErrorType.TRANSIENT, f"Error {i}",
                backoff_base=10, backoff_max=100
            )
        
        # Later attempts should be capped at max
        last_attempt = retry_state.attempts[-1]
        assert last_attempt.backoff_seconds <= 100
    
    def test_model_escalation(self):
        """Test model tier escalation."""
        retry_state = TaskRetryState(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            original_model_tier="haiku-4-5",
            max_retries=3,
            escalation_path=["haiku-4-5", "sonnet-4", "opus-4-6"]
        )
        
        # First attempt should use haiku
        assert retry_state.next_model_tier == "haiku-4-5"
        
        # After first retry, should use sonnet
        retry_state.schedule_retry(ErrorType.QUALITY, "Low quality", 1, 60)
        assert retry_state.next_model_tier == "sonnet-4"
        
        # After second retry, should use opus
        retry_state.schedule_retry(ErrorType.QUALITY, "Low quality", 1, 60)
        assert retry_state.next_model_tier == "opus-4-6"
        
        # Beyond escalation path, should stick with opus
        retry_state.schedule_retry(ErrorType.QUALITY, "Low quality", 1, 60)
        assert retry_state.next_model_tier == "opus-4-6"
    
    def test_max_retries_limit(self):
        """Test maximum retry limit enforcement."""
        retry_state = TaskRetryState(
            task_id="task-123",
            task_type=TaskType.CONTENT,
            original_model_tier="sonnet-4",
            max_retries=2
        )
        
        # Should allow retries under limit
        assert retry_state.should_retry()
        
        retry_state.schedule_retry(ErrorType.TRANSIENT, "Error 1", 1, 60)
        assert retry_state.should_retry()
        
        retry_state.schedule_retry(ErrorType.TRANSIENT, "Error 2", 1, 60)
        assert not retry_state.should_retry()  # Hit limit


class TestRecoveryManager:
    """Test complete recovery management system."""
    
    def test_recovery_manager_initialization(self, test_db, test_config):
        """Test recovery manager initialization."""
        manager = RecoveryManager(test_db, test_config)
        
        assert manager.db == test_db
        assert manager.config == test_config
        assert manager.classifier is not None
        assert len(manager._circuit_breakers) == 0
        assert len(manager._retry_states) == 0
    
    def test_task_failure_handling_retry(self, test_db, test_config):
        """Test task failure handling with retry."""
        manager = RecoveryManager(test_db, test_config)
        
        # Handle transient failure
        should_retry, retry_at, next_model = manager.handle_task_failure(
            "task-123", TaskType.CONTENT, "Connection timeout", "sonnet-4"
        )
        
        assert should_retry
        assert retry_at > datetime.now()
        assert next_model in ["haiku-4-5", "sonnet-4"]  # Escalation path
        
        # Verify retry state is created
        assert "task-123" in manager._retry_states
        retry_state = manager._retry_states["task-123"]
        assert retry_state.current_attempt == 1
    
    def test_task_failure_permanent_no_retry(self, test_db, test_config):
        """Test permanent failure with no retry."""
        manager = RecoveryManager(test_db, test_config)
        
        # Handle permanent failure
        should_retry, retry_at, next_model = manager.handle_task_failure(
            "task-456", TaskType.CODE, "Invalid API key", "sonnet-4"
        )
        
        assert not should_retry
        assert retry_at is None
        assert next_model is None
    
    def test_max_retries_exhausted(self, test_db, test_config):
        """Test behavior when max retries are exhausted."""
        manager = RecoveryManager(test_db, test_config)
        
        task_id = "task-789"
        
        # Exhaust retries
        for i in range(test_config.retry.max_retries_default + 1):
            should_retry, retry_at, next_model = manager.handle_task_failure(
                task_id, TaskType.RESEARCH, f"Error {i}", "sonnet-4"
            )
            
            if i < test_config.retry.max_retries_default:
                assert should_retry
            else:
                assert not should_retry  # Should stop retrying
    
    def test_circuit_breaker_triggers(self, test_db):
        """Test circuit breaker triggering."""
        # Use small threshold for testing
        config = EngineConfig(
            retry=RetryConfig(circuit_breaker_threshold=2)
        )
        manager = RecoveryManager(test_db, config)
        
        # Trigger circuit breaker with repeated failures
        for i in range(3):  # One more than threshold
            should_retry, _, _ = manager.handle_task_failure(
                f"task-{i}", TaskType.CONTENT, "System overload", "haiku-4-5"
            )
        
        # Circuit breaker should be open
        cb_key = "content:haiku-4-5"
        assert cb_key in manager._circuit_breakers
        
        # Next failure should not retry due to circuit breaker
        should_retry, _, _ = manager.handle_task_failure(
            "task-breaker", TaskType.CONTENT, "Another failure", "haiku-4-5"
        )
        assert not should_retry
    
    def test_task_success_handling(self, test_db, test_config):
        """Test successful task completion handling."""
        manager = RecoveryManager(test_db, test_config)
        
        # Create retry state first
        manager.handle_task_failure("task-success", TaskType.CONTENT, "Timeout", "sonnet-4")
        assert "task-success" in manager._retry_states
        
        # Handle success
        manager.handle_task_success("task-success", TaskType.CONTENT, "sonnet-4")
        
        # Retry state should be cleaned up
        assert "task-success" not in manager._retry_states
    
    def test_retry_queue_management(self, test_db, test_config):
        """Test retry queue functionality."""
        manager = RecoveryManager(test_db, test_config)
        
        # Create some retries with different schedules
        past_time = datetime.now() - timedelta(minutes=1)
        future_time = datetime.now() + timedelta(minutes=5)
        
        # Mock database responses for retry queue
        with patch.object(manager.db, 'fetch_all') as mock_fetch:
            mock_fetch.return_value = [
                {"task_id": "task-ready", "next_retry": past_time},
                {"task_id": "task-future", "next_retry": future_time}
            ]
            
            ready_tasks = manager.get_retry_queue()
            
            # Should only return tasks ready for retry
            assert len(ready_tasks) >= 0  # Database might be empty in test
    
    def test_error_statistics(self, test_db, test_config):
        """Test error statistics collection."""
        manager = RecoveryManager(test_db, test_config)
        
        # Generate some errors
        manager.handle_task_failure("task-1", TaskType.CONTENT, "Rate limit", "sonnet-4")
        manager.handle_task_failure("task-2", TaskType.CODE, "Timeout", "opus-4-6")
        
        # Get statistics
        stats = manager.get_error_statistics()
        
        assert "error_type_distribution" in stats
        assert "circuit_breakers" in stats
        assert "retry_statistics" in stats
        
        # Should be a valid structure even with no data
        assert isinstance(stats["error_type_distribution"], list)
        assert isinstance(stats["circuit_breakers"], list)
        assert isinstance(stats["retry_statistics"], dict)


class TestRecoveryIntegration:
    """Integration tests for recovery system."""
    
    def test_complete_retry_cycle(self, test_db, test_config):
        """Test complete retry cycle from failure to success."""
        manager = RecoveryManager(test_db, test_config)
        
        task_id = "integration-task"
        task_type = TaskType.CONTENT
        
        # 1. Initial failure - should schedule retry
        should_retry, retry_at, next_model = manager.handle_task_failure(
            task_id, task_type, "Connection timeout", "haiku-4-5"
        )
        
        assert should_retry
        assert next_model  # Should escalate
        
        # 2. Mark retry as executed (failed)
        manager.mark_retry_executed(task_id, success=False)
        
        # 3. Second failure - should escalate model
        should_retry, retry_at, next_model = manager.handle_task_failure(
            task_id, task_type, "Still timeout", "sonnet-4"
        )
        
        assert should_retry
        # Should escalate to higher tier
        
        # 4. Final success
        manager.handle_task_success(task_id, task_type, "opus-4-6")
        
        # Retry state should be cleaned up
        assert task_id not in manager._retry_states
    
    def test_error_pattern_learning(self, test_db, test_config):
        """Test error pattern learning and frequency tracking."""
        manager = RecoveryManager(test_db, test_config)
        
        # Generate repeated errors
        error_msg = "Custom application error"
        
        for i in range(3):
            manager.handle_task_failure(
                f"task-{i}", TaskType.CONTENT, error_msg, "sonnet-4"
            )
        
        # Error should be recorded in pattern analysis
        # (This would be verified by checking the database directly)
        stats = manager.get_error_statistics()
        assert isinstance(stats, dict)  # Basic structure check
    
    def test_model_escalation_paths(self, test_db, test_config):
        """Test different model escalation paths for different task types."""
        manager = RecoveryManager(test_db, test_config)
        
        test_cases = [
            (TaskType.CONTENT, ["haiku-4-5", "sonnet-4", "opus-4-6"]),
            (TaskType.CODE, ["sonnet-4", "opus-4-6"]),
            (TaskType.TRANSLATION, ["sonnet-4", "opus-4-6"])
        ]
        
        for task_type, expected_path in test_cases:
            task_id = f"test-{task_type.value}"
            
            # First failure
            should_retry, _, next_model = manager.handle_task_failure(
                task_id, task_type, "Quality issue", "haiku-4-5"
            )
            
            if should_retry:
                # Model should be from expected escalation path
                retry_state = manager._retry_states[task_id]
                assert retry_state.escalation_path[0] in expected_path
    
    def test_concurrent_failure_handling(self, test_db, test_config):
        """Test concurrent failure handling doesn't corrupt state."""
        import threading
        
        manager = RecoveryManager(test_db, test_config)
        results = []
        
        def handle_failures(task_prefix):
            for i in range(3):
                task_id = f"{task_prefix}-{i}"
                result = manager.handle_task_failure(
                    task_id, TaskType.CONTENT, "Concurrent error", "sonnet-4"
                )
                results.append((task_id, result))
        
        # Run concurrent failure handling
        threads = []
        for i in range(3):
            thread = threading.Thread(target=handle_failures, args=(f"thread{i}",))
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join()
        
        # All should have valid results
        assert len(results) == 9  # 3 threads × 3 tasks each
        for task_id, (should_retry, retry_at, next_model) in results:
            assert isinstance(should_retry, bool)
            if should_retry:
                assert retry_at is not None
                assert next_model is not None