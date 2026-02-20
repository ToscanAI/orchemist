"""Task Runner - Core Execution Engine for the Orchestration Engine.

The TaskRunner is the central orchestrator that polls the queue for ready tasks,
assigns them to workers, handles execution through various executors, and manages
the complete task lifecycle with error recovery and progress tracking.
"""

import json
import logging
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable
from uuid import uuid4

from .db import Database
from .config import EngineConfig, get_global_config
from .queue import TaskQueue
from .schemas import TaskSpec, TaskResult, TaskState, TaskType, Priority
from .concurrency import WorkerPool
from .recovery import RecoveryManager, ErrorType
from .progress import ProgressTracker, ProgressEventType, ProgressEvent


logger = logging.getLogger(__name__)


class TaskExecutor(ABC):
    """Abstract base class for task executors."""
    
    @abstractmethod
    def execute(self, task: TaskSpec, worker_id: str, model_tier: str = None,
                thinking_level: str = None) -> TaskResult:
        """Execute a task and return the result.
        
        Args:
            task: Task specification
            worker_id: ID of executing worker
            model_tier: Model tier to use (haiku, sonnet, opus)
            thinking_level: Thinking level for the model
            
        Returns:
            TaskResult with execution outcome
        """
        pass
    
    @abstractmethod
    def can_handle(self, task_type: TaskType) -> bool:
        """Check if this executor can handle the given task type."""
        pass
    
    @abstractmethod
    def estimate_cost(self, task: TaskSpec) -> float:
        """Estimate the cost of executing this task in USD."""
        pass


class DryRunExecutor(TaskExecutor):
    """Dry run executor for testing - returns mock results."""
    
    def __init__(self, delay_seconds: float = 2.0, failure_rate: float = 0.1):
        """Initialize dry run executor.
        
        Args:
            delay_seconds: Simulated execution time
            failure_rate: Probability of simulated failure (0.0 to 1.0)
        """
        self.delay_seconds = delay_seconds
        self.failure_rate = failure_rate
    
    def execute(self, task: TaskSpec, worker_id: str, model_tier: str = None,
                thinking_level: str = None) -> TaskResult:
        """Execute task with mock behavior."""
        import random
        
        start_time = datetime.now()
        
        # Simulate processing time
        time.sleep(self.delay_seconds)
        
        # Simulate occasional failures
        if random.random() < self.failure_rate:
            return TaskResult(
                task_id=task.id if hasattr(task, 'id') else str(uuid4()),
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[{
                    "code": "dry_run_failure",
                    "message": "Simulated failure for testing",
                    "severity": "error"
                }],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used=model_tier or "dry-run",
                execution_time_seconds=(datetime.now() - start_time).total_seconds()
            )
        
        # Success case
        return TaskResult(
            task_id=task.id if hasattr(task, 'id') else str(uuid4()),
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.85,
            result={
                "message": f"Mock execution of {task.type.value} task",
                "model_used": model_tier or "dry-run",
                "worker_id": worker_id,
                "payload_size": len(str(task.payload))
            },
            started_at=start_time,
            completed_at=datetime.now(),
            model_used=model_tier or "dry-run",
            tokens_consumed=random.randint(100, 1000),
            execution_time_seconds=(datetime.now() - start_time).total_seconds(),
            cost_usd=random.uniform(0.01, 0.10)
        )
    
    def can_handle(self, task_type: TaskType) -> bool:
        """Dry run executor can handle all task types."""
        return True
    
    def estimate_cost(self, task: TaskSpec) -> float:
        """Estimate mock cost."""
        return 0.05  # Mock cost estimate


class LocalExecutor(TaskExecutor):
    """Executor for tasks that run locally (shell commands, scripts)."""
    
    def __init__(self, allowed_commands: List[str] = None):
        """Initialize local executor.
        
        Args:
            allowed_commands: List of allowed command prefixes for security
        """
        self.allowed_commands = allowed_commands or ['echo', 'ls', 'cat', 'python']
    
    def execute(self, task: TaskSpec, worker_id: str, model_tier: str = None,
                thinking_level: str = None) -> TaskResult:
        """Execute local command or script."""
        start_time = datetime.now()
        
        try:
            # Extract command from payload
            command = task.payload.get('command')
            if not command:
                raise ValueError("No 'command' specified in task payload")
            
            # Parse command safely — no shell interpretation
            import shlex
            cmd_parts = shlex.split(command)
            if not cmd_parts:
                raise ValueError("Empty command after parsing")
            
            # Security check: validate the executable (first token only)
            executable = cmd_parts[0]
            if not any(executable == allowed for allowed in self.allowed_commands):
                raise ValueError(f"Command not allowed: {executable}")
            
            # Execute command without shell — prevents injection
            result = subprocess.run(
                cmd_parts,
                shell=False,
                capture_output=True,
                text=True,
                timeout=task.timeout_seconds or 300
            )
            
            # Build result
            if result.returncode == 0:
                return TaskResult(
                    task_id=task.id if hasattr(task, 'id') else str(uuid4()),
                    task_type=task.type,
                    state=TaskState.SUCCESS,
                    confidence=1.0,
                    result={
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "return_code": result.returncode,
                        "command": command
                    },
                    started_at=start_time,
                    completed_at=datetime.now(),
                    model_used="local-executor",
                    execution_time_seconds=(datetime.now() - start_time).total_seconds()
                )
            else:
                return TaskResult(
                    task_id=task.id if hasattr(task, 'id') else str(uuid4()),
                    task_type=task.type,
                    state=TaskState.FAILED,
                    confidence=0.0,
                    result={},
                    errors=[{
                        "code": "command_failed",
                        "message": f"Command failed with exit code {result.returncode}: {result.stderr}",
                        "severity": "error"
                    }],
                    started_at=start_time,
                    completed_at=datetime.now(),
                    model_used="local-executor",
                    execution_time_seconds=(datetime.now() - start_time).total_seconds()
                )
        
        except subprocess.TimeoutExpired:
            return TaskResult(
                task_id=task.id if hasattr(task, 'id') else str(uuid4()),
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[{
                    "code": "timeout",
                    "message": f"Command timed out after {task.timeout_seconds} seconds",
                    "severity": "error"
                }],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used="local-executor",
                execution_time_seconds=(datetime.now() - start_time).total_seconds()
            )
        
        except Exception as e:
            return TaskResult(
                task_id=task.id if hasattr(task, 'id') else str(uuid4()),
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[{
                    "code": "execution_error",
                    "message": str(e),
                    "severity": "error"
                }],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used="local-executor",
                execution_time_seconds=(datetime.now() - start_time).total_seconds()
            )
    
    def can_handle(self, task_type: TaskType) -> bool:
        """Local executor handles specific task types with local commands."""
        return task_type in [TaskType.CODE, TaskType.REVIEW]
    
    def estimate_cost(self, task: TaskSpec) -> float:
        """Local execution is free."""
        return 0.0


class OpenClawExecutor(TaskExecutor):
    """Executor that formats tasks for OpenClaw sub-agents."""

    def __init__(self, config: EngineConfig, dry_run: bool = True):
        """Initialize OpenClaw executor.

        Args:
            config: Engine configuration
            dry_run: When True (default), use the built-in simulation for
                testing and development.  Set to False in production to invoke
                the real _execute_via_openclaw() path.
        """
        self.config = config
        self.dry_run = dry_run
    
    def execute(self, task: TaskSpec, worker_id: str, model_tier: str = None,
                thinking_level: str = None) -> TaskResult:
        """Execute task via OpenClaw sessions_spawn().
        
        This method formats the task into a clean prompt and uses OpenClaw's
        subprocess interface to spawn a sub-agent for execution.
        """
        start_time = datetime.now()
        task_id = task.id if hasattr(task, 'id') else str(uuid4())
        
        try:
            # Format task into OpenClaw-compatible prompt
            prompt = self._format_task_prompt(task)
            
            # Get model configuration
            model_name = self.config.models.tier_mappings.get(
                model_tier or self.config.models.default_tier,
                "anthropic/claude-sonnet-4-20250514"
            )
            
            thinking = self.config.models.thinking_levels.get(
                model_tier or self.config.models.default_tier
            ) or thinking_level
            
            # Build OpenClaw command
            cmd = ["sessions_spawn"]
            cmd.extend(["--model", model_name])
            
            if thinking:
                cmd.extend(["--thinking", thinking])
            
            # Add timeout
            timeout_seconds = task.timeout_seconds or self.config.resources.default_timeout_seconds
            cmd.extend(["--timeout", str(timeout_seconds)])
            
            # Add the prompt
            cmd.append(prompt)
            
            logger.info(f"Executing task {task_id} with OpenClaw: model={model_name}, thinking={thinking}")

            # Execute via real OpenClaw interface (or simulation if dry_run)
            if self.dry_run:
                result = self._simulate_openclaw_execution(cmd, task, timeout_seconds)
            else:
                result = self._execute_via_openclaw(task, model_name, thinking or "low", timeout_seconds)
            
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.SUCCESS if result['success'] else TaskState.FAILED,
                confidence=result.get('confidence', 0.7),
                result=result.get('output', {}),
                errors=result.get('errors', []),
                started_at=start_time,
                completed_at=datetime.now(),
                model_used=model_name,
                tokens_consumed=result.get('tokens_used', 0),
                execution_time_seconds=(datetime.now() - start_time).total_seconds(),
                cost_usd=result.get('cost_usd', 0.0)
            )
        
        except Exception as e:
            logger.error(f"OpenClaw execution failed for task {task_id}: {e}")
            
            return TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[{
                    "code": "openclaw_error",
                    "message": str(e),
                    "severity": "error"
                }],
                started_at=start_time,
                completed_at=datetime.now(),
                model_used=model_tier or "unknown",
                execution_time_seconds=(datetime.now() - start_time).total_seconds()
            )
    
    def _format_task_prompt(self, task: TaskSpec) -> str:
        """Format task into a clean prompt for OpenClaw."""
        prompt_parts = []
        
        # Task type specific prompt formatting
        if task.type == TaskType.CONTENT:
            prompt_parts.append(f"Create content based on the following specification:")
            prompt_parts.append(json.dumps(task.payload, indent=2))
            
        elif task.type == TaskType.CODE:
            prompt_parts.append(f"Write code based on the following requirements:")
            prompt_parts.append(json.dumps(task.payload, indent=2))
            
        elif task.type == TaskType.RESEARCH:
            prompt_parts.append(f"Conduct research on the following topic:")
            prompt_parts.append(json.dumps(task.payload, indent=2))
            
        elif task.type == TaskType.TRANSLATION:
            prompt_parts.append(f"Translate the following content:")
            prompt_parts.append(json.dumps(task.payload, indent=2))
            
        elif task.type == TaskType.REVIEW:
            prompt_parts.append(f"Review and analyze the following:")
            prompt_parts.append(json.dumps(task.payload, indent=2))
        
        else:
            prompt_parts.append(f"Execute the following {task.type.value} task:")
            prompt_parts.append(json.dumps(task.payload, indent=2))
        
        # Add quality requirements
        if task.min_confidence > 0:
            prompt_parts.append(f"\nQuality requirement: Minimum confidence level {task.min_confidence}")
        
        # Add any specific instructions
        if 'instructions' in task.payload:
            prompt_parts.append(f"\nAdditional instructions: {task.payload['instructions']}")
        
        return "\n".join(prompt_parts)
    
    def _resolve_model(self, model_tier: str = None) -> str:
        """Resolve model name from tier, falling back to configured default."""
        return self.config.models.tier_mappings.get(
            model_tier or self.config.models.default_tier,
            "anthropic/claude-sonnet-4-6"
        )

    def _resolve_thinking(self, model_tier: str = None,
                          thinking_level: str = None) -> str:
        """Resolve thinking level from tier or explicit override."""
        return (
            thinking_level
            or self.config.models.thinking_levels.get(
                model_tier or self.config.models.default_tier
            )
            or "low"
        )

    def _build_success_dict(self, output_data: Dict[str, Any],
                             model_name: str) -> Dict[str, Any]:
        """Build a success result dict from parsed output_data."""
        return {
            "success": True,
            "confidence": output_data.get("confidence", 0.75),
            "output": output_data.get("output", output_data),
            "tokens_used": output_data.get("tokens_used", 0),
            "cost_usd": output_data.get("cost_usd", 0.0),
        }

    def _build_failure_dict(self, message: str) -> Dict[str, Any]:
        """Build a failure result dict."""
        return {
            "success": False,
            "confidence": 0.0,
            "output": {},
            "errors": [{"code": "openclaw_error", "message": message,
                        "severity": "error"}],
            "tokens_used": 0,
            "cost_usd": 0.0,
        }

    def _poll_for_result(self, output_file: Path, status_file: Path,
                          model_name: str, timeout: int) -> Dict[str, Any]:
        """Poll for output.json until it appears or timeout expires."""
        poll_interval = 2.0
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if output_file.exists():
                try:
                    output_data = json.loads(output_file.read_text())
                    status_file.write_text("completed")
                    return self._build_success_dict(output_data, model_name)
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning(f"Error reading output file: {exc}")

            time.sleep(poll_interval)

        status_file.write_text("timeout")
        return self._build_failure_dict("Timed out waiting for output file")

    def _execute_via_openclaw(self, task: TaskSpec, model_name: str,
                              thinking: str, timeout: int) -> Dict[str, Any]:
        """Execute task via OpenClaw file-based contract.

        Strategy:
        1. Write input.json + status file to ~/.orchestration-engine/tasks/<id>/
        2. Try to run the ``openclaw agent run`` CLI command.
        3. If the CLI isn't installed (FileNotFoundError) fall back to polling
           for output.json — another process (OpenClaw itself) will produce it.
        4. On subprocess timeout mark the task as timed-out.
        """
        task_id = task.id if hasattr(task, "id") else str(uuid4())
        
        # Sanitize task_id to prevent path traversal
        import re as _re
        if not _re.match(r'^[a-zA-Z0-9_-]+$', task_id):
            task_id = str(uuid4())  # Replace unsafe IDs with a fresh UUID
        
        prompt = self._format_task_prompt(task)

        # 1. Create task directory & write input
        tasks_root = Path.home() / ".orchestration-engine" / "tasks"
        task_dir = tasks_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        
        # Verify resolved path is under tasks_root (defense in depth)
        if not task_dir.resolve().is_relative_to(tasks_root.resolve()):
            raise ValueError(f"Task directory escaped root: {task_id}")

        input_file = task_dir / "input.json"
        output_file = task_dir / "output.json"
        status_file = task_dir / "status"

        input_data = {
            "task_id": task_id,
            "prompt": prompt,
            "model": model_name,
            "thinking": thinking,
            "timeout_seconds": timeout,
            "task_type": task.type.value,
            "created_at": datetime.now().isoformat(),
        }
        input_file.write_text(json.dumps(input_data, indent=2, default=str))
        status_file.write_text("pending")

        logger.info(f"Task {task_id} written to {input_file}")

        # 2. Try OpenClaw CLI
        try:
            cli_result = subprocess.run(
                [
                    "openclaw", "agent", "run",
                    "--model", model_name,
                    "--prompt", prompt[:10000],  # CLI arg limit
                    "--output", str(output_file),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(task_dir),
            )

            if output_file.exists():
                output_data = json.loads(output_file.read_text())
                status_file.write_text("completed")
                return self._build_success_dict(output_data, model_name)
            else:
                status_file.write_text("failed")
                return self._build_failure_dict(
                    f"No output file produced. stderr: {cli_result.stderr[:500]}"
                )

        except FileNotFoundError:
            # OpenClaw CLI not installed — poll for output.json written by
            # an external OpenClaw process that monitors the task directory.
            logger.info(
                f"openclaw CLI not found; falling back to file polling for task {task_id}"
            )
            status_file.write_text("waiting")
            return self._poll_for_result(output_file, status_file, model_name, timeout)

        except subprocess.TimeoutExpired:
            status_file.write_text("timeout")
            return self._build_failure_dict("Execution timed out")

    def _simulate_openclaw_execution(self, cmd: List[str], task: TaskSpec,
                                     timeout: int) -> Dict[str, Any]:
        """Simulate OpenClaw execution for development/testing.
        
        In production, this would be replaced with actual subprocess.run()
        calling the real OpenClaw sessions_spawn command.
        """
        import random
        import time
        
        # Simulate execution delay
        time.sleep(random.uniform(1.0, 3.0))
        
        # Simulate success/failure based on task type
        success_rates = {
            TaskType.CONTENT: 0.9,
            TaskType.CODE: 0.8,
            TaskType.RESEARCH: 0.85,
            TaskType.TRANSLATION: 0.95,
            TaskType.REVIEW: 0.9
        }
        
        success_rate = success_rates.get(task.type, 0.85)
        success = random.random() < success_rate
        
        if success:
            return {
                'success': True,
                'confidence': random.uniform(0.7, 0.95),
                'output': {
                    'result': f"Simulated {task.type.value} result",
                    'model_used': cmd[2] if len(cmd) > 2 else "unknown",
                    'task_id': task.id if hasattr(task, 'id') else str(uuid4())
                },
                'tokens_used': random.randint(200, 2000),
                'cost_usd': random.uniform(0.02, 0.20)
            }
        else:
            return {
                'success': False,
                'confidence': 0.0,
                'output': {},
                'errors': [{
                    'code': 'simulated_failure',
                    'message': 'Simulated OpenClaw execution failure',
                    'severity': 'error'
                }],
                'tokens_used': random.randint(50, 500),
                'cost_usd': random.uniform(0.01, 0.05)
            }
    
    def can_handle(self, task_type: TaskType) -> bool:
        """OpenClaw executor can handle all task types."""
        return True
    
    def estimate_cost(self, task: TaskSpec) -> float:
        """Estimate cost based on task type and model tier."""
        # Cost estimates per 1K tokens (approximate)
        costs = {
            "haiku-4-5": 0.0003,
            "sonnet-4": 0.003,
            "opus-4-6": 0.015
        }
        
        # Estimate token usage based on payload size
        payload_size = len(str(task.payload))
        estimated_tokens = max(100, payload_size * 2)  # Rough estimate
        
        model_cost = costs.get(
            self.config.models.default_tier,
            costs["sonnet-4"]
        )
        
        return (estimated_tokens / 1000) * model_cost


class TaskRunner:
    """Main task runner that orchestrates the complete execution pipeline."""
    
    def __init__(self, database: Database = None, config: EngineConfig = None):
        """Initialize the task runner.
        
        Args:
            database: Database instance (uses default if None)
            config: Configuration instance (uses default if None)
        """
        self.config = config or get_global_config()
        self.db = database or Database()
        
        # Core components
        self.queue = TaskQueue(self.db)
        self.worker_pool = WorkerPool(self.db, self.config)
        self.recovery_manager = RecoveryManager(self.db, self.config)
        self.progress_tracker = ProgressTracker(self.db)
        
        # Task executors
        self.executors: List[TaskExecutor] = []
        self._init_executors()
        
        # Control flags
        self._running = False
        self._runner_thread: Optional[threading.Thread] = None
        
        logger.info("TaskRunner initialized")
    
    def _init_executors(self) -> None:
        """Initialize task executors based on configuration."""
        # Always add dry run executor for testing
        if self.config.dry_run:
            self.executors.append(DryRunExecutor())
            logger.info("Added DryRunExecutor (dry run mode)")
        else:
            # Production executors — OpenClawExecutor with real execution enabled
            self.executors.extend([
                LocalExecutor(),
                OpenClawExecutor(self.config, dry_run=False)
            ])
            logger.info("Added LocalExecutor and OpenClawExecutor")
    
    def start(self) -> None:
        """Start the task runner."""
        if self._running:
            logger.warning("TaskRunner already running")
            return
        
        self._running = True
        
        # Start worker pool
        self.worker_pool.start()
        
        # Start runner thread
        self._runner_thread = threading.Thread(
            target=self._runner_loop,
            name="TaskRunner-Main",
            daemon=True
        )
        self._runner_thread.start()
        
        logger.info("TaskRunner started")
    
    def stop(self) -> None:
        """Stop the task runner."""
        if not self._running:
            return
        
        logger.info("Stopping TaskRunner...")
        
        self._running = False
        
        # Stop worker pool
        self.worker_pool.stop()
        
        # Wait for runner thread to complete
        if self._runner_thread and self._runner_thread.is_alive():
            self._runner_thread.join(timeout=10)
        
        logger.info("TaskRunner stopped")
    
    def _runner_loop(self) -> None:
        """Main runner loop that processes tasks."""
        logger.info("TaskRunner main loop started")
        
        while self._running:
            try:
                # Process retry queue first
                self._process_retry_queue()
                
                # Process new tasks
                self._process_new_tasks()
                
                # Sleep before next poll
                time.sleep(self.config.queue.poll_interval_seconds)
                
            except Exception as e:
                logger.error(f"Error in runner loop: {e}", exc_info=True)
                time.sleep(5)  # Brief pause on error
        
        logger.info("TaskRunner main loop ended")
    
    def _process_retry_queue(self) -> None:
        """Process tasks ready for retry."""
        retry_tasks = self.recovery_manager.get_retry_queue()
        
        for retry_info in retry_tasks:
            task_id = retry_info['task_id']
            
            try:
                # Get task from queue
                task = self.queue.get_task(task_id)
                if not task:
                    logger.warning(f"Retry task {task_id} not found in queue")
                    continue
                
                # Check if we have capacity
                if self.worker_pool.get_available_capacity() <= 0:
                    logger.debug("No worker capacity for retry tasks")
                    break
                
                # Process the retry
                self._execute_task(task, is_retry=True)
                
            except Exception as e:
                logger.error(f"Error processing retry for task {task_id}: {e}")
    
    def _process_new_tasks(self) -> None:
        """Process new tasks from the queue."""
        # Get available capacity
        capacity = self.worker_pool.get_available_capacity()
        if capacity <= 0:
            return
        
        # Get ready tasks
        ready_tasks = self.queue.get_ready_tasks(limit=capacity)
        
        for task in ready_tasks:
            try:
                self._execute_task(task)
            except Exception as e:
                logger.error(f"Error executing task {task.id}: {e}")
    
    def _execute_task(self, task: TaskSpec, is_retry: bool = False) -> None:
        """Execute a single task.
        
        Args:
            task: Task to execute
            is_retry: Whether this is a retry attempt
        """
        task_id = task.id if hasattr(task, 'id') else str(uuid4())
        
        # Assign to worker
        worker_id = self.worker_pool.assign_task(task_id)
        if not worker_id:
            logger.warning(f"Could not assign task {task_id} to worker")
            return
        
        # Record task started
        if not is_retry:
            self.progress_tracker.task_queued(task_id)
        
        self.progress_tracker.task_started(task_id, worker_id)
        
        # Execute in background thread to avoid blocking main loop
        execution_thread = threading.Thread(
            target=self._execute_task_in_worker,
            args=(task, worker_id, is_retry),
            name=f"TaskExecution-{task_id[:8]}",
            daemon=True
        )
        execution_thread.start()
    
    def _execute_task_in_worker(self, task: TaskSpec, worker_id: str, is_retry: bool) -> None:
        """Execute task in worker thread.
        
        Args:
            task: Task to execute
            worker_id: Worker executing the task
            is_retry: Whether this is a retry attempt
        """
        task_id = task.id if hasattr(task, 'id') else str(uuid4())
        
        try:
            # Find appropriate executor
            executor = self._select_executor(task.type)
            if not executor:
                raise ValueError(f"No executor available for task type {task.type}")
            
            # Determine model tier
            model_tier = self._select_model_tier(task, is_retry)
            thinking_level = self.config.models.thinking_levels.get(model_tier)
            
            # Start execution
            session_id = f"session-{uuid4().hex[:8]}"
            self.worker_pool.start_task_execution(worker_id, session_id)
            
            self.progress_tracker.record_event(ProgressEvent(
                task_id=task_id,
                event_type=ProgressEventType.MODEL_SELECTED,
                message=f"Selected model tier: {model_tier}",
                model_tier=model_tier,
                worker_id=worker_id,
                attempt_number=task.retry_count + 1 if hasattr(task, 'retry_count') else 1
            ))
            
            # Execute task
            result = executor.execute(task, worker_id, model_tier, thinking_level)
            
            # Handle result
            if result.state == TaskState.SUCCESS:
                self._handle_task_success(task, result, worker_id, model_tier)
            else:
                self._handle_task_failure(task, result, worker_id, model_tier)
        
        except Exception as e:
            logger.error(f"Task execution error for {task_id}: {e}", exc_info=True)
            
            # Create error result
            error_result = TaskResult(
                task_id=task_id,
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[{
                    "code": "execution_exception",
                    "message": str(e),
                    "severity": "critical"
                }],
                started_at=datetime.now(),
                completed_at=datetime.now(),
                model_used="unknown"
            )
            
            self._handle_task_failure(task, error_result, worker_id, "unknown")
        
        finally:
            # Always complete the worker task
            self.worker_pool.complete_task(worker_id, success=True)
    
    def _select_executor(self, task_type: TaskType) -> Optional[TaskExecutor]:
        """Select appropriate executor for task type."""
        for executor in self.executors:
            if executor.can_handle(task_type):
                return executor
        
        return None
    
    def _select_model_tier(self, task: TaskSpec, is_retry: bool) -> str:
        """Select model tier for task execution."""
        if task.preferred_model:
            return task.preferred_model.value
        
        if is_retry:
            # Use escalation path for retries
            attempt_num = task.retry_count + 1 if hasattr(task, 'retry_count') else 2
            from .schemas import select_model_tier
            return select_model_tier(task.type, attempt_num).value
        
        return self.config.models.default_tier
    
    def _handle_task_success(self, task: TaskSpec, result: TaskResult, 
                           worker_id: str, model_tier: str) -> None:
        """Handle successful task completion."""
        task_id = task.id if hasattr(task, 'id') else result.task_id
        
        # Update queue
        self.queue.complete_task(task_id, result)
        
        # Record progress
        self.progress_tracker.task_completed(
            task_id, worker_id, result.tokens_consumed, str(result.cost_usd)
        )
        
        # Notify recovery manager
        self.recovery_manager.handle_task_success(task_id, task.type, model_tier)
        
        try:
            confidence_str = f"{float(result.confidence):.2f}"
            cost_str = f"{float(result.cost_usd or 0):.4f}"
        except (TypeError, ValueError):
            confidence_str = str(result.confidence)
            cost_str = str(result.cost_usd)
        logger.info(
            f"Task {task_id} completed successfully (model: {model_tier}, "
            f"confidence: {confidence_str}, cost: ${cost_str})"
        )
    
    def _handle_task_failure(self, task: TaskSpec, result: TaskResult,
                           worker_id: str, model_tier: str) -> None:
        """Handle task failure and determine retry strategy."""
        task_id = task.id if hasattr(task, 'id') else result.task_id
        
        # Extract error message — errors may be TaskError objects or raw dicts
        error_message = "Unknown error"
        if result.errors:
            first = result.errors[0]
            if isinstance(first, dict):
                error_message = first.get('message', error_message)
            else:
                error_message = getattr(first, 'message', error_message)
        
        # Handle with recovery manager
        should_retry, retry_at, next_model = self.recovery_manager.handle_task_failure(
            task_id, task.type, error_message, model_tier
        )
        
        if should_retry and retry_at:
            # Schedule retry
            self.queue.schedule_retry(task_id, retry_at, next_model)
            self.progress_tracker.task_retry_scheduled(
                task_id, retry_at, 
                task.retry_count + 1 if hasattr(task, 'retry_count') else 1,
                error_message, worker_id
            )
            
            logger.info(f"Task {task_id} scheduled for retry at {retry_at} with model {next_model}")
        else:
            # Permanent failure
            self.queue.fail_task(task_id, result)
            self.progress_tracker.task_failed(
                task_id, error_message, worker_id,
                task.retry_count + 1 if hasattr(task, 'retry_count') else 1,
                is_permanent=True
            )
            
            logger.warning(f"Task {task_id} permanently failed: {error_message}")
    
    def execute_task_immediately(self, task_id: str) -> bool:
        """Execute a specific task immediately, bypassing the queue.
        
        Args:
            task_id: Task ID to execute
            
        Returns:
            True if task was started, False otherwise
        """
        task = self.queue.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        
        if self.worker_pool.get_available_capacity() <= 0:
            logger.error("No worker capacity available for immediate execution")
            return False
        
        try:
            self._execute_task(task)
            return True
        except Exception as e:
            logger.error(f"Failed to execute task {task_id} immediately: {e}")
            return False
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive runner status.
        
        Returns:
            Dictionary with runner status information
        """
        return {
            "running": self._running,
            "worker_pool": self.worker_pool.get_worker_status(),
            "queue_stats": self.queue.get_queue_stats(),
            "recovery_stats": self.recovery_manager.get_error_statistics(),
            "active_tasks": self.progress_tracker.get_active_tasks(),
            "executors": [
                {
                    "type": type(executor).__name__,
                    "can_handle": [task_type.value for task_type in TaskType if executor.can_handle(task_type)]
                }
                for executor in self.executors
            ]
        }