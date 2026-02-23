"""Simple task result types for lightweight executors (fallback, OpenAI-compatible, etc.).

This module provides a minimal TaskResult dataclass suitable for executors that
don't need the full Pydantic schema (cost tracking, token counts, etc.).
TaskState is re-exported from schemas for convenience.
"""
from dataclasses import dataclass, field
from typing import Optional

from .schemas import TaskState

__all__ = ["ExecutorResult", "TaskState"]


@dataclass
class ExecutorResult:
    """Lightweight task result for simple/fallback executors."""

    state: TaskState
    output: str = ""
    worker_id: str = "worker"
    error_code: str = ""
    duration_seconds: float = 0.0
