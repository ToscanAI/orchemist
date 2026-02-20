"""Tests for the Anthropic API executor."""

import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from orchestration_engine.executors.anthropic_executor import AnthropicExecutor, _MODEL_MAP
from orchestration_engine.schemas import TaskSpec, TaskType, TaskState, ModelTier, Priority


@pytest.fixture
def executor():
    """Create an executor with a test API key."""
    return AnthropicExecutor(api_key="sk-ant-test-key")


@pytest.fixture
def sample_task():
    """Create a basic task for testing."""
    return TaskSpec(
        type=TaskType.CONTENT,
        payload={"prompt": "Write a haiku about testing."},
        priority=Priority.NORMAL,
    )


class TestAnthropicExecutorInit:
    """Test executor initialization."""

    def test_init_with_api_key(self):
        ex = AnthropicExecutor(api_key="sk-ant-test")
        assert ex.api_key == "sk-ant-test"

    def test_init_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
        ex = AnthropicExecutor()
        assert ex.api_key == "sk-ant-env"

    def test_init_no_key_warns(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        ex = AnthropicExecutor(api_key="")
        assert ex.api_key == ""

    def test_can_handle_all_types(self, executor):
        for task_type in TaskType:
            assert executor.can_handle(task_type) is True

    def test_estimate_cost_tiers(self, executor, sample_task):
        # Default (sonnet) cost
        cost = executor.estimate_cost(sample_task)
        assert cost > 0


class TestModelMapping:
    """Test model tier resolution."""

    def test_haiku_maps(self):
        assert _MODEL_MAP[ModelTier.HAIKU] == "claude-haiku-4-5-20251001"

    def test_sonnet_maps(self):
        assert _MODEL_MAP[ModelTier.SONNET] == "claude-sonnet-4-6"

    def test_opus_maps(self):
        assert _MODEL_MAP[ModelTier.OPUS] == "claude-opus-4-6"

    def test_string_fallbacks(self):
        assert _MODEL_MAP["haiku"] == "claude-haiku-4-5-20251001"
        assert _MODEL_MAP["sonnet"] == "claude-sonnet-4-6"
        assert _MODEL_MAP["opus"] == "claude-opus-4-6"


class TestExecuteSuccess:
    """Test successful execution paths."""

    def _mock_response(self, text="Test response", input_tokens=100, output_tokens=50):
        return {
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        }

    @patch.object(AnthropicExecutor, "_call_api")
    def test_basic_execution(self, mock_api, executor, sample_task):
        mock_api.return_value = self._mock_response()
        result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert result.confidence == 0.8
        assert result.result["text"] == "Test response"
        assert result.tokens_consumed == 150
        assert result.model_used == "claude-sonnet-4-6"

    @patch.object(AnthropicExecutor, "_call_api")
    def test_haiku_tier(self, mock_api, executor, sample_task):
        mock_api.return_value = self._mock_response()
        result = executor.execute(sample_task, model_tier="haiku")

        assert result.model_used == "claude-haiku-4-5-20251001"

    @patch.object(AnthropicExecutor, "_call_api")
    def test_opus_tier(self, mock_api, executor, sample_task):
        mock_api.return_value = self._mock_response()
        result = executor.execute(sample_task, model_tier="opus")

        assert result.model_used == "claude-opus-4-6"

    @patch.object(AnthropicExecutor, "_call_api")
    def test_json_output_parsed(self, mock_api, executor, sample_task):
        mock_api.return_value = self._mock_response(
            text='{"key": "value", "count": 42}'
        )
        result = executor.execute(sample_task)

        assert result.result["key"] == "value"
        assert result.result["count"] == 42

    @patch.object(AnthropicExecutor, "_call_api")
    def test_json_in_code_block(self, mock_api, executor, sample_task):
        mock_api.return_value = self._mock_response(
            text='Here is the result:\n```json\n{"key": "value"}\n```'
        )
        result = executor.execute(sample_task)

        assert result.result["key"] == "value"

    @patch.object(AnthropicExecutor, "_call_api")
    def test_plain_text_output(self, mock_api, executor, sample_task):
        mock_api.return_value = self._mock_response(text="Just plain text")
        result = executor.execute(sample_task)

        assert result.result["text"] == "Just plain text"

    @patch.object(AnthropicExecutor, "_call_api")
    def test_cost_calculated(self, mock_api, executor, sample_task):
        mock_api.return_value = self._mock_response(
            input_tokens=1000, output_tokens=500
        )
        result = executor.execute(sample_task, model_tier="sonnet")

        # Sonnet: $3/M input + $15/M output
        expected = (1000 * 3.0 + 500 * 15.0) / 1_000_000
        assert abs(float(result.cost_usd) - expected) < 0.0001

    @patch.object(AnthropicExecutor, "_call_api")
    def test_execution_time_recorded(self, mock_api, executor, sample_task):
        mock_api.return_value = self._mock_response()
        result = executor.execute(sample_task)

        assert result.execution_time_seconds >= 0
        assert result.started_at is not None
        assert result.completed_at is not None


class TestExecuteFailure:
    """Test error handling paths."""

    @patch.object(AnthropicExecutor, "_call_api")
    def test_api_error_returns_failed(self, mock_api, executor, sample_task):
        mock_api.side_effect = RuntimeError("Anthropic API error 429: rate limited")
        result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.confidence == 0.0
        assert len(result.errors) == 1
        assert "429" in result.errors[0].message

    @patch.object(AnthropicExecutor, "_call_api")
    def test_timeout_returns_failed(self, mock_api, executor, sample_task):
        mock_api.side_effect = TimeoutError("Connection timed out")
        result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED


class TestThinking:
    """Test extended thinking configuration."""

    @patch.object(AnthropicExecutor, "_call_api")
    def test_thinking_off(self, mock_api, executor, sample_task):
        mock_api.return_value = {
            "content": [{"type": "text", "text": "result"}],
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
        executor.execute(sample_task, thinking_level="off")

        call_body = mock_api.call_args[0][0]
        assert "thinking" not in call_body

    @patch.object(AnthropicExecutor, "_call_api")
    def test_thinking_medium(self, mock_api, executor, sample_task):
        mock_api.return_value = {
            "content": [{"type": "text", "text": "result"}],
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
        executor.execute(sample_task, thinking_level="medium")

        call_body = mock_api.call_args[0][0]
        assert call_body["thinking"]["type"] == "enabled"
        assert call_body["thinking"]["budget_tokens"] == 8192


class TestJsonParsing:
    """Test JSON extraction from various response formats."""

    def test_raw_json(self):
        result = AnthropicExecutor._try_parse_json('{"a": 1}')
        assert result == {"a": 1}

    def test_json_code_block(self):
        result = AnthropicExecutor._try_parse_json('```json\n{"a": 1}\n```')
        assert result == {"a": 1}

    def test_json_generic_code_block(self):
        result = AnthropicExecutor._try_parse_json('```\n{"a": 1}\n```')
        assert result == {"a": 1}

    def test_plain_text_returns_none(self):
        result = AnthropicExecutor._try_parse_json("Just text")
        assert result is None

    def test_invalid_json_returns_none(self):
        result = AnthropicExecutor._try_parse_json("{not valid json}")
        assert result is None
