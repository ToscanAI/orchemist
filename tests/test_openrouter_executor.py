"""Tests for OpenRouterExecutor — behavioral acceptance tests.

Tests the OpenRouter executor against its behavioral contracts:
model tier resolution, thinking support, error handling, cost tracking,
and PipelineRunner integration.
"""

import json
import sys
import urllib.error
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestration_engine.executors.openrouter_executor import (
    DEFAULT_MODEL_MAP,
    OpenRouterExecutor,
)
from orchestration_engine.schemas import TaskSpec, TaskState, TaskType


def _make_task(prompt="Write a hello world", task_type=TaskType.CODE, disable_tools=True):
    """Create a minimal TaskSpec for testing.

    ``disable_tools=True`` by default so this legacy test suite exercises the
    single-shot code path whose error codes (auth_error, rate_limit, overloaded,
    bad_request, timeout, empty_response) it was written against. The new
    tool-loop path in #794 uses a different error-code taxonomy by design.
    """
    return TaskSpec(
        type=task_type,
        payload={"prompt": prompt, "disable_tools": disable_tools},
    )


def _mock_response(content="Generated output", prompt_tokens=100,
                   completion_tokens=200, total_cost=None):
    """Create a mock OpenRouter API response."""
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if total_cost is not None:
        usage["total_cost"] = total_cost

    return json.dumps({
        "choices": [{"message": {"content": content}}],
        "usage": usage,
    }).encode("utf-8")


def _mock_urlopen(response_bytes):
    """Create a mock for urllib.request.urlopen that returns the given bytes."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_bytes
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestModelTierResolution:
    """Test that model_tier resolves to the correct OpenRouter model ID."""

    def test_sonnet_resolves_to_anthropic_sonnet(self):
        """Given model_tier='sonnet', resolves to anthropic/claude-sonnet-4-6."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="sonnet")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert sent_body["model"] == "anthropic/claude-sonnet-4-6"

    def test_opus_resolves_to_anthropic_opus(self):
        """Given model_tier='opus', resolves to anthropic/claude-opus-4-8.

        The OPUS tier now emits opus-4-8 (maintainer-authorized model upgrade,
        #916 registry). Same $5/$25 price as 4.6/4.7.
        """
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="opus")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert sent_body["model"] == "anthropic/claude-opus-4-8"

    def test_haiku_resolves_to_anthropic_haiku(self):
        """Given model_tier='haiku', resolves to anthropic/claude-haiku-4-5-20251001.

        The stale dotted id (anthropic/claude-haiku-4.5) is purged from
        DEFAULT_MODEL_MAP — it had no pricing.yaml key and silently billed at
        the sonnet default (#916/#913).
        """
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="haiku")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert sent_body["model"] == "anthropic/claude-haiku-4-5-20251001"

    def test_unknown_tier_passes_through_as_literal(self):
        """Given unknown model_tier, passes it directly as model name."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="meta-llama/llama-3.3-70b")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert sent_body["model"] == "meta-llama/llama-3.3-70b"

    def test_custom_model_map_overrides_defaults(self):
        """Given a custom model_map, uses it instead of defaults."""
        executor = OpenRouterExecutor(
            api_key="sk-or-test",
            model_map={"sonnet": "openai/gpt-4o"},
        )
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="sonnet")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert sent_body["model"] == "openai/gpt-4o"


class TestHappyPath:
    """Test successful execution flow."""

    def test_successful_execution_returns_success(self):
        """Given a successful API response, returns TaskResult with SUCCESS."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(
                _mock_response(content="Hello world!", prompt_tokens=50, completion_tokens=100)
            )
            result = executor.execute(task, model_tier="sonnet")
            assert result.state == TaskState.SUCCESS
            assert result.result["output"] == "Hello world!"
            assert mock_url.call_count == 1

    def test_token_counts_extracted(self):
        """Given usage data in response, token counts are extracted."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(
                _mock_response(prompt_tokens=150, completion_tokens=300)
            )
            result = executor.execute(task, model_tier="sonnet")
            assert result.metadata["prompt_tokens"] == 150
            assert result.metadata["completion_tokens"] == 300

    def test_task_prompt_included_in_request(self):
        """Given a task with a prompt, the prompt is sent in the API request."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task(prompt="Generate a fibonacci function")
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="sonnet")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert sent_body["messages"][0]["content"] == "Generate a fibonacci function"


class TestCostTracking:
    """Test cost tracking from OpenRouter responses."""

    def test_total_cost_from_api_used_when_present(self):
        """Given usage.total_cost in response, uses it directly."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(
                _mock_response(total_cost=0.0042)
            )
            result = executor.execute(task, model_tier="sonnet")
            assert float(result.cost_usd) == pytest.approx(0.0042)

    def test_cost_estimated_when_total_cost_absent(self):
        """Given no usage.total_cost, the cost is computed via PricingTable.

        The former blended per-tier `$/1K` heuristic was removed (#913/#916);
        the no-`total_cost` path now prices the prompt/completion tokens with
        first-party Anthropic rates. Sonnet (the default tier) at 500 in +
        500 out = 500*$3/Mtok + 500*$15/Mtok = 0.009.
        """
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(
                _mock_response(prompt_tokens=500, completion_tokens=500, total_cost=None)
            )
            result = executor.execute(task, model_tier="sonnet")
            assert float(result.cost_usd) == pytest.approx(0.009)


class TestThinkingSupport:
    """Test extended thinking parameter handling."""

    def test_thinking_included_for_anthropic_models(self):
        """Given thinking_level='high' and Anthropic model, includes thinking param."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="sonnet", thinking_level="high")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert "thinking" in sent_body
            assert sent_body["thinking"]["type"] == "enabled"
            assert sent_body["thinking"]["budget_tokens"] > 0

    def test_thinking_not_included_for_non_anthropic(self):
        """Given thinking_level='high' but non-Anthropic model, no thinking param."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="openai/gpt-4o", thinking_level="high")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert "thinking" not in sent_body

    def test_thinking_off_excludes_param(self):
        """Given thinking_level='off', no thinking param included."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="sonnet", thinking_level="off")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert "thinking" not in sent_body

    def test_thinking_none_excludes_param(self):
        """Given thinking_level=None, no thinking param included."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="sonnet", thinking_level=None)
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert "thinking" not in sent_body

    def test_thinking_silently_ignored_for_passthrough_model(self):
        """Given an unknown passthrough model, thinking is silently ignored."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="meta-llama/llama-3.3-70b", thinking_level="high")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert "thinking" not in sent_body


class TestThinkingRetry:
    """Test the thinking-400 retry mechanism."""

    def test_retries_once_without_thinking_on_400(self):
        """Given 400 with thinking enabled, retries exactly once without thinking."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()

        error_body = json.dumps({"error": {"message": "thinking not supported"}}).encode()
        http_error = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/chat/completions",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=BytesIO(error_body),
        )

        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call has thinking — verify
                sent_body = json.loads(args[0].data)
                assert "thinking" in sent_body
                raise http_error
            return _mock_urlopen(_mock_response())

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = executor.execute(task, model_tier="sonnet", thinking_level="high")
            assert result.state == TaskState.SUCCESS
            assert call_count == 2  # exactly 1 retry


class TestErrorHandling:
    """Test HTTP error mapping."""

    def test_401_returns_auth_error(self):
        """Given HTTP 401, returns FAILED with error_code 'auth_error'."""
        executor = OpenRouterExecutor(api_key="sk-or-bad")
        task = _make_task()
        error_body = json.dumps({"error": {"message": "Invalid API key"}}).encode()
        http_error = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/chat/completions",
            code=401, msg="Unauthorized", hdrs=None,
            fp=BytesIO(error_body),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            result = executor.execute(task, model_tier="sonnet")
            assert result.state == TaskState.FAILED
            assert any(e.code == "auth_error" for e in result.errors)

    def test_429_returns_rate_limit(self):
        """Given HTTP 429, returns FAILED with error_code 'rate_limit'."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        error_body = json.dumps({"error": {"message": "Rate limit"}}).encode()
        http_error = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/chat/completions",
            code=429, msg="Too Many Requests", hdrs=None,
            fp=BytesIO(error_body),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            result = executor.execute(task, model_tier="sonnet")
            assert result.state == TaskState.FAILED
            assert any(e.code == "rate_limit" for e in result.errors)

    def test_502_returns_overloaded(self):
        """Given HTTP 502, returns FAILED with error_code 'overloaded'."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        error_body = json.dumps({"error": {"message": "Bad Gateway"}}).encode()
        http_error = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/chat/completions",
            code=502, msg="Bad Gateway", hdrs=None,
            fp=BytesIO(error_body),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            result = executor.execute(task, model_tier="sonnet")
            assert result.state == TaskState.FAILED
            assert any(e.code == "overloaded" for e in result.errors)

    def test_503_returns_overloaded(self):
        """Given HTTP 503, returns FAILED with error_code 'overloaded'."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        error_body = json.dumps({"error": {"message": "Service Unavailable"}}).encode()
        http_error = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/chat/completions",
            code=503, msg="Service Unavailable", hdrs=None,
            fp=BytesIO(error_body),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            result = executor.execute(task, model_tier="sonnet")
            assert result.state == TaskState.FAILED
            assert any(e.code == "overloaded" for e in result.errors)

    def test_400_returns_bad_request_with_api_detail(self):
        """Given HTTP 400, returns FAILED with 'bad_request' and API error message."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        error_body = json.dumps({"error": {"message": "Invalid model: fake/model"}}).encode()
        http_error = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/chat/completions",
            code=400, msg="Bad Request", hdrs=None,
            fp=BytesIO(error_body),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            result = executor.execute(task, model_tier="sonnet")
            assert result.state == TaskState.FAILED
            assert any(e.code == "bad_request" for e in result.errors)
            assert any("Invalid model" in e.message for e in result.errors)

    def test_network_timeout_returns_timeout(self):
        """Given a network timeout, returns FAILED with error_code 'timeout'."""
        executor = OpenRouterExecutor(api_key="sk-or-test", timeout_seconds=1)
        task = _make_task()
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = executor.execute(task, model_tier="sonnet")
            assert result.state == TaskState.FAILED
            assert any(e.code == "timeout" for e in result.errors)

    def test_empty_choices_returns_empty_response(self):
        """Given empty choices array, returns FAILED with 'empty_response'."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task()
        empty_resp = json.dumps({"choices": [], "usage": {}}).encode()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(empty_resp)
            result = executor.execute(task, model_tier="sonnet")
            assert result.state == TaskState.FAILED
            assert any(e.code == "empty_response" for e in result.errors)


class TestConfiguration:
    """Test configuration and env var handling."""

    def test_env_var_fallback_for_api_key(self):
        """Given OPENROUTER_API_KEY env var, uses it when no explicit key."""
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-env"}):
            executor = OpenRouterExecutor()
            assert executor.api_key == "sk-or-env"

    def test_explicit_key_overrides_env(self):
        """Given explicit api_key and env var, uses explicit."""
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-env"}):
            executor = OpenRouterExecutor(api_key="sk-or-explicit")
            assert executor.api_key == "sk-or-explicit"

    def test_custom_base_url(self):
        """Given custom base_url, uses it for API calls."""
        executor = OpenRouterExecutor(api_key="sk-or-test", base_url="https://my-proxy.com/v1")
        task = _make_task()
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response())
            executor.execute(task, model_tier="sonnet")
            request_url = mock_url.call_args[0][0].full_url
            assert request_url.startswith("https://my-proxy.com/v1/")

    def test_can_handle_returns_true_for_all_types(self):
        """The executor accepts all task types (fast-paths command types locally)."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        for task_type in TaskType:
            assert executor.can_handle(task_type) is True

    def test_command_task_type_runs_locally_without_llm(self):
        """COMMAND tasks run via subprocess, not LLM — zero tokens, zero cost."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task(prompt="unused", task_type=TaskType.COMMAND, disable_tools=False)
        task.payload["command"] = "echo hello-from-local"
        task.payload["working_dir"] = "/tmp"
        result = executor.execute(task, model_tier="sonnet")
        assert result.model_used == "local-subprocess"
        assert result.tokens_consumed == 0
        assert result.cost_usd == 0
        assert "hello-from-local" in (result.result or {}).get("output", "")
        assert result.metadata.get("exit_code") == 0

    def test_command_task_type_nonzero_exit_returns_failed(self):
        """COMMAND with non-zero exit → TaskState.FAILED + error details."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task(prompt="unused", task_type=TaskType.COMMAND, disable_tools=False)
        task.payload["command"] = "exit 42"
        task.payload["working_dir"] = "/tmp"
        result = executor.execute(task, model_tier="sonnet")
        assert result.state == TaskState.FAILED
        assert result.model_used == "local-subprocess"
        assert result.tokens_consumed == 0
        assert result.metadata.get("exit_code") == 42

    def test_acceptance_run_without_command_falls_through_to_llm(self):
        """ACCEPTANCE_RUN with no command field falls through to the LLM path (not fast-pathed)."""
        executor = OpenRouterExecutor(api_key="sk-or-test")
        task = _make_task(prompt="test prompt", task_type=TaskType.ACCEPTANCE_RUN, disable_tools=True)
        # No "command" key in payload → should NOT fast-path → uses LLM (disable_tools=True → single-shot)
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_mock_response(content="llm response"))
            result = executor.execute(task, model_tier="sonnet")
        assert result.model_used != "local-subprocess", (
            "ACCEPTANCE_RUN without command should fall through to LLM, not fast-path"
        )

    def test_configurable_timeout(self):
        """Given timeout_seconds, uses it for HTTP calls."""
        executor = OpenRouterExecutor(api_key="sk-or-test", timeout_seconds=60)
        assert executor.timeout_seconds == 60


class TestPipelineRunnerIntegration:
    """Test PipelineRunner.openrouter() factory method."""

    def test_openrouter_factory_creates_runner(self):
        """PipelineRunner.openrouter() creates a working runner."""
        from orchestration_engine.pipeline_runner import PipelineRunner
        runner = PipelineRunner.openrouter(api_key="sk-or-test")
        assert runner is not None
        assert len(runner.executors) == 1

    def test_openrouter_factory_raises_without_key(self):
        """PipelineRunner.openrouter() raises ValueError without API key."""
        from orchestration_engine.pipeline_runner import PipelineRunner
        with patch.dict("os.environ", {}, clear=True):
            # Clear any existing OPENROUTER_API_KEY
            import os
            os.environ.pop("OPENROUTER_API_KEY", None)
            with pytest.raises(ValueError, match="OpenRouter API key required"):
                PipelineRunner.openrouter()
