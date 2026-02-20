"""Executors — pluggable backends for running pipeline phases."""

from .anthropic_executor import AnthropicExecutor

__all__ = ["AnthropicExecutor"]
