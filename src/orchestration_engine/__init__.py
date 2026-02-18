"""Orchestration Engine - AI Agent Task Coordination.

A Python CLI tool for multi-agent task orchestration on top of OpenClaw.
Provides task queuing, retry logic, quality gates, and reusable orchestra templates.
"""

__version__ = "0.1.0"
__author__ = "Toscan Rivera"
__email__ = "toscan@example.com"

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
]