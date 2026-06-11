"""Task executor primitives for the Orchestration Engine.

Defines the :class:`TaskExecutor` abstract base class implemented by every
executor in the engine, plus :class:`DryRunExecutor` — a deterministic, no-API
executor used by tests and CI. Concrete production executors live in their own
modules (``openclaw_executor``, ``executors/anthropic_executor``, etc.).
"""

# Long lines below are prompt/instruction string literals black cannot wrap
# (this module is prompt-template-dense); a file-level header avoids per-line noise.
# ruff: noqa: E501

import logging
import time
from abc import ABC, abstractmethod
from uuid import uuid4

from .schemas import TaskResult, TaskSpec, TaskState, TaskType
from .timestamps import now_utc

logger = logging.getLogger(__name__)


class TaskExecutor(ABC):
    """Abstract base class for task executors."""

    #: Per-phase provider identity (#969). Default "" = unknown/abstract; the
    #: provider-aware resolver falls back to class-name substring for executors
    #: that do not set a non-empty value.
    provider_name: str = ""

    @abstractmethod
    def execute(
        self, task: TaskSpec, worker_id: str, model_tier: str = None, thinking_level: str = None
    ) -> TaskResult:
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


def _dry_run_synthetic_text(task_type: "TaskType") -> str:
    """Return a deterministic synthetic text string for dry-run phase output.

    The returned string satisfies all downstream consumers:
    - First non-blank line is exactly ``APPROVE`` (passes verdict parsers and
      spec-adversary output consumers).
    - Contains ``(dry-run)`` so observers can distinguish synthetic from real output.
    - Is deterministic: given the same task_type, always returns the same string.

    Args:
        task_type: The TaskType enum value for the phase being simulated.

    Returns:
        A synthetic plain-prose string safe for all routing consumers.
    """
    # Task-type-specific body text (makes logs more readable without affecting routing)
    _BODY_MAP = {  # noqa: N806
        "content": "Synthetic content output generated in dry-run mode. The requested content has been produced according to the provided instructions.",
        "code": "Synthetic code output generated in dry-run mode. The implementation matches the specification and all edge cases are handled.",
        "research": "Synthetic research output generated in dry-run mode. The topic has been researched and relevant findings are documented.",
        "translation": "Synthetic translation output generated in dry-run mode. The source text has been accurately translated into the target language.",
        "review": "Synthetic review output generated in dry-run mode. All submitted materials have been reviewed and feedback is incorporated.",
        "analysis": "Synthetic analysis output generated in dry-run mode. The data has been analysed and key insights are documented.",
        "triage": "Synthetic triage output generated in dry-run mode. Issues have been categorised and prioritised appropriately.",
        "compliance": "Synthetic compliance output generated in dry-run mode. All regulatory requirements have been verified and documented.",
        "financial": "Synthetic financial output generated in dry-run mode. The financial data has been processed and results are accurate.",
        "sales": "Synthetic sales output generated in dry-run mode. Customer requirements have been addressed and next steps are clear.",
        "support": "Synthetic support output generated in dry-run mode. The support request has been resolved satisfactorily.",
        "command": "Synthetic command output generated in dry-run mode. The requested command has been executed and output captured.",
        "acceptance_run": "Synthetic acceptance-run output generated in dry-run mode. All acceptance tests have been executed and results recorded.",
    }
    # Resolve body from map; fall back to a generic body for unknown/future types
    type_value = task_type.value if hasattr(task_type, "value") else str(task_type)
    body = _BODY_MAP.get(
        type_value,
        "Synthetic output generated in dry-run mode. The task has been executed successfully.",
    )

    return f"APPROVE\n\n{body} (dry-run)"


class DryRunExecutor(TaskExecutor):
    """Dry run executor for testing - returns mock results."""

    provider_name = "dryrun"  # per-phase provider identity (#969)

    def __init__(self, delay_seconds: float = 2.0, failure_rate: float = 0.1):
        """Initialize dry run executor.

        Args:
            delay_seconds: Simulated execution time
            failure_rate: Probability of simulated failure (0.0 to 1.0)
        """
        self.delay_seconds = delay_seconds
        self.failure_rate = failure_rate

    def execute(
        self,
        task: TaskSpec,
        worker_id: str,
        model_tier: str = None,
        thinking_level: str = None,  # noqa: ARG002
    ) -> TaskResult:
        """Execute task with mock behavior."""
        import random  # noqa: PLC0415

        start_time = now_utc()

        # Simulate processing time
        time.sleep(self.delay_seconds)

        # Simulate occasional failures
        if random.random() < self.failure_rate:
            return TaskResult(
                task_id=task.id if hasattr(task, "id") else str(uuid4()),
                task_type=task.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={},
                errors=[
                    {
                        "code": "dry_run_failure",
                        "message": "Simulated failure for testing",
                        "severity": "error",
                    }
                ],
                started_at=start_time,
                completed_at=now_utc(),
                model_used=model_tier or "dry-run",
                execution_time_seconds=(now_utc() - start_time).total_seconds(),
            )

        # Success case
        return TaskResult(
            task_id=task.id if hasattr(task, "id") else str(uuid4()),
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.85,
            result={
                "text": _dry_run_synthetic_text(task.type),
                "message": f"Mock execution of {task.type.value} task",
                "model_used": model_tier or "dry-run",
                "worker_id": worker_id,
                "payload_size": len(str(task.payload)),
                "dry_run": True,
            },
            started_at=start_time,
            completed_at=now_utc(),
            model_used=model_tier or "dry-run",
            tokens_consumed=random.randint(100, 1000),
            execution_time_seconds=(now_utc() - start_time).total_seconds(),
            cost_usd=random.uniform(0.01, 0.10),
        )

    def can_handle(self, task_type: TaskType) -> bool:  # noqa: ARG002
        """Dry run executor can handle all task types."""
        return True

    def estimate_cost(self, task: TaskSpec) -> float:  # noqa: ARG002
        """Estimate mock cost."""
        return 0.05  # Mock cost estimate
