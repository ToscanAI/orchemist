"""Executors — pluggable backends for running pipeline phases."""

from .anthropic_executor import AnthropicExecutor
from .claudecode_executor import ClaudeCodeExecutor
from .gemini_cli_executor import GeminiCliExecutor

__all__ = ["AnthropicExecutor", "ClaudeCodeExecutor", "GeminiCliExecutor"]
