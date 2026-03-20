"""Executors — pluggable backends for running pipeline phases."""

from .anthropic_executor import AnthropicExecutor
from .claudecode_executor import ClaudeCodeExecutor

__all__ = ["AnthropicExecutor", "ClaudeCodeExecutor"]
