"""Tests for the single-sourced OpenRouter timeout default (#919, item 3).

``_DEFAULT_OR_TIMEOUT`` lives in ``orchestration_engine.config`` and is shared by
``OpenRouterConfig.timeout_seconds`` (the model default) and
``OpenRouterExecutor.__init__`` (the constructor default) so the two cannot
drift. The executor constructor default was 600; it is now the shared 300.
Production instantiates via ``PipelineRunner.openrouter`` which already passes
300 explicitly, so production timeout behavior is unchanged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestration_engine import config
from orchestration_engine.config import OpenRouterConfig, _DEFAULT_OR_TIMEOUT
from orchestration_engine.executors.openrouter_executor import OpenRouterExecutor


def test_shared_constant_is_300():
    assert _DEFAULT_OR_TIMEOUT == 300
    assert config._DEFAULT_OR_TIMEOUT == 300


def test_openrouter_config_default_timeout():
    assert OpenRouterConfig().timeout_seconds == 300


def test_executor_constructor_default_timeout():
    """The executor default now reads the shared constant (was hardcoded 600)."""
    assert OpenRouterExecutor(api_key="sk-or-test").timeout_seconds == 300


def test_config_and_executor_defaults_agree():
    assert OpenRouterConfig().timeout_seconds == _DEFAULT_OR_TIMEOUT
    assert OpenRouterExecutor(api_key="sk-or-test").timeout_seconds == _DEFAULT_OR_TIMEOUT
