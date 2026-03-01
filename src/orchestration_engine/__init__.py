"""Orchestration Engine - AI Agent Task Coordination.

A Python CLI tool for multi-agent task orchestration on top of OpenClaw.
Provides task queuing, retry logic, quality gates, and reusable orchestra templates.
"""

__version__ = "0.1.0"
__author__ = "Conny Lazo"
__email__ = "contact@renerivera.net"

from .schemas import (
    TaskSpec,
    TaskStatus,
    TaskResult,
    TaskSummary,
    OrchestraSpec,
    OrchestraStatus,
    QueueStats,
    Priority,
    TaskType,
    TaskState,
)
from .queue import TaskQueue
from .db import Database
from .sequencer import StateMachineSequencer

__all__ = [
    "__version__",
    "__author__",
    "__email__",
    # Schemas
    "TaskSpec",
    "TaskStatus", 
    "TaskResult",
    "TaskSummary",
    "OrchestraSpec",
    "OrchestraStatus",
    "QueueStats",
    "Priority",
    "TaskType",
    "TaskState",
    # Core classes
    "TaskQueue",
    "Database",
    "StateMachineSequencer",
]
