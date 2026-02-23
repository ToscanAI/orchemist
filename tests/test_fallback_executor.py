"""Tests for OpenAICompatibleExecutor, FallbackHandler, and pipeline_runner wiring.

All tests use mocked HTTP — no real network calls are made.
~30 tests covering:
- OpenAICompatibleExecutor: dry_run, success, empty response, connection error,
  timeout, invalid JSON
- FallbackHandler: primary succeeds, retriable fallback, non-retriable passthrough,
  no fallback config (passthrough)
- Model/URL configuration
- CLI: --mode standalone still works without fallback
"""

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import urllib.error
import urllib.request

import pytest
from click.testing import CliRunner

from orchestration_engine.executor import ExecutorResult, TaskState
from orchestration_engine.openai_executor import OpenAICompatibleExecutor
from orchestration_engine.fallback import FallbackHandler, RETRIABLE_ERRORS
from orchestration_engine.pipeline_runner import PipelineRunner
from orchestration_engine.templates import PipelineTemplate, PhaseDefinition
from orchestration_engine.cli import main


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_response(content: str, status: int = 200) -> BytesIO:
    """Build a fake urllib response body that returns *content*."""
    payload = json.dumps(
        {"choices": [{"message": {"content": content}}]}
    ).encode()
    return BytesIO(payload)


class _FakeHTTPResponse:
    """Minimal file-like object that urlopen's context manager expects."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _fake_resp(content: str) -> _FakeHTTPResponse:
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    return _FakeHTTPResponse(body)


@pytest.fixture
def executor():
    return OpenAICompatibleExecutor(
        base_url="http://localhost:8765/v1",
        model="gemini-3-pro-preview",
        api_key="test-key",
    )


@pytest.fixture
def dry_executor():
    return OpenAICompatibleExecutor(
        base_url="http://localhost:8765/v1",
        dry_run=True,
    )


@pytest.fixture
def hello_yaml(tmp_path) -> Path:
    src = Path(__file__).parent.parent / "examples" / "hello-pipeline.yaml"
    dest = tmp_path / "hello-pipeline.yaml"
    dest.write_text(src.read_text())
    return dest


# ---------------------------------------------------------------------------
# OpenAICompatibleExecutor — dry run
# ---------------------------------------------------------------------------


class TestOpenAIExecutorDryRun:
    def test_dry_run_returns_success(self, dry_executor):
        result = dry_executor.execute("Do something")
        assert result.state == TaskState.SUCCESS

    def test_dry_run_output_contains_prefix(self, dry_executor):
        result = dry_executor.execute("Write a haiku")
        assert result.output.startswith("[DRY RUN] Fallback:")

    def test_dry_run_output_truncates_task(self, dry_executor):
        long_task = "x" * 200
        result = dry_executor.execute(long_task)
        # Should truncate to 100 chars plus "..."
        assert len(result.output) < 130

    def test_dry_run_worker_id_preserved(self, dry_executor):
        result = dry_executor.execute("task", worker_id="my-worker")
        assert result.worker_id == "my-worker"

    def test_dry_run_no_http_calls(self, dry_executor):
        with patch("urllib.request.urlopen") as mock_open:
            dry_executor.execute("task")
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# OpenAICompatibleExecutor — success path
# ---------------------------------------------------------------------------


class TestOpenAIExecutorSuccess:
    def test_success_returns_success_state(self, executor):
        with patch("urllib.request.urlopen", return_value=_fake_resp("Hello world")):
            result = executor.execute("Say hello")
        assert result.state == TaskState.SUCCESS

    def test_success_output_matches_response(self, executor):
        with patch("urllib.request.urlopen", return_value=_fake_resp("Model output")):
            result = executor.execute("prompt")
        assert result.output == "Model output"

    def test_success_duration_positive(self, executor):
        with patch("urllib.request.urlopen", return_value=_fake_resp("ok")):
            result = executor.execute("prompt")
        assert result.duration_seconds >= 0.0

    def test_success_worker_id_default(self, executor):
        with patch("urllib.request.urlopen", return_value=_fake_resp("ok")):
            result = executor.execute("prompt")
        assert result.worker_id == "fallback"

    def test_success_custom_worker_id(self, executor):
        with patch("urllib.request.urlopen", return_value=_fake_resp("ok")):
            result = executor.execute("prompt", worker_id="unit-tester")
        assert result.worker_id == "unit-tester"

    def test_success_no_error_code(self, executor):
        with patch("urllib.request.urlopen", return_value=_fake_resp("ok")):
            result = executor.execute("prompt")
        assert result.error_code == ""


# ---------------------------------------------------------------------------
# OpenAICompatibleExecutor — empty response
# ---------------------------------------------------------------------------


class TestOpenAIExecutorEmptyResponse:
    def _empty_resp(self):
        body = json.dumps({"choices": [{"message": {"content": ""}}]}).encode()
        return _FakeHTTPResponse(body)

    def test_empty_response_is_failed(self, executor):
        with patch("urllib.request.urlopen", return_value=self._empty_resp()):
            result = executor.execute("prompt")
        assert result.state == TaskState.FAILED

    def test_empty_response_error_code(self, executor):
        with patch("urllib.request.urlopen", return_value=self._empty_resp()):
            result = executor.execute("prompt")
        assert result.error_code == "empty_response"

    def test_missing_choices_key(self, executor):
        body = json.dumps({}).encode()
        resp = _FakeHTTPResponse(body)
        with patch("urllib.request.urlopen", return_value=resp):
            result = executor.execute("prompt")
        # Empty choices → empty content → FAILED
        assert result.state == TaskState.FAILED
        assert result.error_code == "empty_response"


# ---------------------------------------------------------------------------
# OpenAICompatibleExecutor — connection error
# ---------------------------------------------------------------------------


class TestOpenAIExecutorConnectionError:
    def test_url_error_returns_failed(self, executor):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = executor.execute("prompt")
        assert result.state == TaskState.FAILED

    def test_url_error_code(self, executor):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            result = executor.execute("prompt")
        assert result.error_code == "connection_error"

    def test_url_error_output_contains_reason(self, executor):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("host unreachable"),
        ):
            result = executor.execute("prompt")
        assert "Connection error" in result.output


# ---------------------------------------------------------------------------
# OpenAICompatibleExecutor — timeout
# ---------------------------------------------------------------------------


class TestOpenAIExecutorTimeout:
    def test_timeout_returns_failed(self, executor):
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            result = executor.execute("prompt")
        assert result.state == TaskState.FAILED

    def test_timeout_error_code(self, executor):
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            result = executor.execute("prompt")
        assert result.error_code == "timeout"


# ---------------------------------------------------------------------------
# OpenAICompatibleExecutor — invalid JSON
# ---------------------------------------------------------------------------


class TestOpenAIExecutorInvalidJSON:
    def test_invalid_json_returns_failed(self, executor):
        bad = _FakeHTTPResponse(b"not json at all }{")
        with patch("urllib.request.urlopen", return_value=bad):
            result = executor.execute("prompt")
        assert result.state == TaskState.FAILED

    def test_invalid_json_error_code(self, executor):
        bad = _FakeHTTPResponse(b"bad{}")
        with patch("urllib.request.urlopen", return_value=bad):
            result = executor.execute("prompt")
        assert result.error_code == "invalid_response"


# ---------------------------------------------------------------------------
# OpenAICompatibleExecutor — model / URL configuration
# ---------------------------------------------------------------------------


class TestOpenAIExecutorConfiguration:
    def test_model_attribute(self):
        ex = OpenAICompatibleExecutor(model="my-model")
        assert ex.model == "my-model"

    def test_base_url_trailing_slash_stripped(self):
        ex = OpenAICompatibleExecutor(base_url="http://host:1234/v1/")
        assert ex.base_url == "http://host:1234/v1"

    def test_api_key_stored(self):
        ex = OpenAICompatibleExecutor(api_key="secret-key")
        assert ex.api_key == "secret-key"

    def test_timeout_stored(self):
        ex = OpenAICompatibleExecutor(timeout_seconds=60)
        assert ex.timeout_seconds == 60

    def test_can_handle_any_type(self):
        ex = OpenAICompatibleExecutor()
        assert ex.can_handle("content") is True
        assert ex.can_handle("anything") is True

    def test_estimate_cost_zero(self):
        ex = OpenAICompatibleExecutor()
        assert ex.estimate_cost("some task") == 0.0

    def test_request_uses_correct_url(self, executor):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _fake_resp("ok")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            executor.execute("prompt")

        assert captured["url"] == "http://localhost:8765/v1/chat/completions"

    def test_request_uses_model(self, executor):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _fake_resp("ok")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            executor.execute("prompt")

        assert captured["body"]["model"] == "gemini-3-pro-preview"

    def test_request_auth_header(self, executor):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return _fake_resp("ok")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            executor.execute("prompt")

        assert captured["auth"] == "Bearer test-key"


# ---------------------------------------------------------------------------
# FallbackHandler
# ---------------------------------------------------------------------------


class TestFallbackHandler:
    def _make_primary(self, state=TaskState.SUCCESS, error_code=""):
        primary = MagicMock()
        primary.execute.return_value = ExecutorResult(
            state=state,
            output="primary output",
            worker_id="primary",
            error_code=error_code,
        )
        return primary

    def test_primary_success_no_fallback_called(self):
        primary = self._make_primary(TaskState.SUCCESS)
        handler = FallbackHandler(primary, fallback_config=None)
        result = handler.execute("task")
        assert result.state == TaskState.SUCCESS
        assert result.output == "primary output"

    def test_no_fallback_config_passthrough(self):
        primary = self._make_primary(TaskState.FAILED, error_code="rate_limit")
        handler = FallbackHandler(primary, fallback_config=None)
        result = handler.execute("task")
        # No fallback — primary failure is returned as-is
        assert result.state == TaskState.FAILED
        assert result.error_code == "rate_limit"

    def test_retriable_error_triggers_fallback(self):
        primary = self._make_primary(TaskState.FAILED, error_code="rate_limit")
        fallback_config = {"base_url": "http://localhost:8765/v1", "model": "gemini-3-pro-preview"}
        handler = FallbackHandler(primary, fallback_config=fallback_config)

        with patch("urllib.request.urlopen", return_value=_fake_resp("fallback output")):
            result = handler.execute("task", worker_id="w1")

        assert result.state == TaskState.SUCCESS
        assert result.output == "fallback output"

    def test_fallback_worker_id_has_suffix(self):
        primary = self._make_primary(TaskState.FAILED, error_code="timeout")
        fallback_config = {"base_url": "http://localhost:8765/v1"}
        handler = FallbackHandler(primary, fallback_config=fallback_config)

        with patch("urllib.request.urlopen", return_value=_fake_resp("ok")):
            result = handler.execute("task", worker_id="my-worker")

        assert result.worker_id == "my-worker-fallback"

    def test_non_retriable_error_no_fallback(self):
        primary = self._make_primary(TaskState.FAILED, error_code="bad_request")
        fallback_config = {"base_url": "http://localhost:8765/v1"}
        handler = FallbackHandler(primary, fallback_config=fallback_config)

        with patch("urllib.request.urlopen") as mock_open:
            result = handler.execute("task")

        mock_open.assert_not_called()
        assert result.state == TaskState.FAILED
        assert result.error_code == "bad_request"

    def test_overloaded_triggers_fallback(self):
        primary = self._make_primary(TaskState.FAILED, error_code="overloaded")
        fallback_config = {"base_url": "http://localhost:8765/v1"}
        handler = FallbackHandler(primary, fallback_config=fallback_config)

        with patch("urllib.request.urlopen", return_value=_fake_resp("ok")):
            result = handler.execute("task")

        assert result.state == TaskState.SUCCESS

    def test_retriable_errors_constant(self):
        assert "rate_limit" in RETRIABLE_ERRORS
        assert "timeout" in RETRIABLE_ERRORS
        assert "overloaded" in RETRIABLE_ERRORS

    def test_fallback_config_model_propagated(self):
        primary = self._make_primary(TaskState.FAILED, error_code="rate_limit")
        fallback_config = {
            "base_url": "http://localhost:8765/v1",
            "model": "custom-model-xyz",
        }
        handler = FallbackHandler(primary, fallback_config=fallback_config)
        assert handler.fallback is not None
        assert handler.fallback.model == "custom-model-xyz"

    def test_no_fallback_config_fallback_is_none(self):
        primary = self._make_primary()
        handler = FallbackHandler(primary, fallback_config=None)
        assert handler.fallback is None


# ---------------------------------------------------------------------------
# PipelineRunner — fallback wiring
# ---------------------------------------------------------------------------


class TestPipelineRunnerFallback:
    def test_standalone_no_fallback_config_none(self):
        runner = PipelineRunner.standalone(api_key="sk-ant-test")
        assert runner.fallback_config is None
        runner.close()

    def test_standalone_with_fallback_config_stored(self):
        cfg = {"base_url": "http://localhost:8765/v1", "model": "gemini"}
        runner = PipelineRunner.standalone(api_key="sk-ant-test", fallback_config=cfg)
        assert runner.fallback_config == cfg
        runner.close()

    def test_template_fallback_none_by_default(self):
        tpl = PipelineTemplate(id="t", name="Test")
        assert tpl.fallback is None


# ---------------------------------------------------------------------------
# CLI — --mode standalone still works without fallback
# ---------------------------------------------------------------------------


class TestCliStandaloneNoFallback:
    def test_standalone_with_mocked_api_no_fallback(self, hello_yaml):
        mock_response = {
            "content": [{"type": "text", "text": "Hello from standalone"}],
            "usage": {"input_tokens": 5, "output_tokens": 10},
        }
        from orchestration_engine.executors.anthropic_executor import AnthropicExecutor

        with patch.object(AnthropicExecutor, "_call_api", return_value=mock_response):
            runner_obj = CliRunner()
            result = runner_obj.invoke(
                main,
                [
                    "run",
                    str(hello_yaml),
                    "--mode",
                    "standalone",
                    "--api-key",
                    "sk-ant-test",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert "completed" in result.output

    def test_dry_run_still_works(self, hello_yaml):
        runner_obj = CliRunner()
        result = runner_obj.invoke(
            main,
            ["run", str(hello_yaml), "--mode", "dry-run"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "Pipeline completed" in result.output
