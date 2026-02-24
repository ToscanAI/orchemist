"""Tests for the OpenClawExecutor class and CLI wiring."""

import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch, call
import urllib.error

import pytest
from click.testing import CliRunner

from orchestration_engine.openclaw_executor import (
    MODEL_MAP,
    THINKING_MAP,
    OpenClawExecutor,
)
from orchestration_engine.schemas import (
    ModelTier,
    Priority,
    TaskSpec,
    TaskState,
    TaskType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def executor():
    """An executor pointed at a mock gateway (no real HTTP)."""
    return OpenClawExecutor(
        gateway_url="http://localhost:18789",
        gateway_token="test-token",
    )


@pytest.fixture
def dry_executor():
    """An executor in dry-run mode — never makes HTTP calls."""
    return OpenClawExecutor(
        gateway_url="http://localhost:18789",
        dry_run=True,
    )


@pytest.fixture
def sample_task():
    """A basic content task."""
    return TaskSpec(
        type=TaskType.CONTENT,
        payload={"prompt": "Write a haiku about testing."},
        priority=Priority.NORMAL,
    )


# ---------------------------------------------------------------------------
# Module-level constant tests
# ---------------------------------------------------------------------------


class TestModelMap:
    """MODEL_MAP must cover all tiers with correct model IDs."""

    def test_haiku_string(self):
        assert MODEL_MAP["haiku"] == "anthropic/claude-haiku-4-5-20251001"

    def test_sonnet_string(self):
        assert MODEL_MAP["sonnet"] == "anthropic/claude-sonnet-4-6"

    def test_opus_string(self):
        assert MODEL_MAP["opus"] == "anthropic/claude-opus-4-6"

    def test_haiku_enum(self):
        assert MODEL_MAP[ModelTier.HAIKU] == "anthropic/claude-haiku-4-5-20251001"

    def test_sonnet_enum(self):
        assert MODEL_MAP[ModelTier.SONNET] == "anthropic/claude-sonnet-4-6"

    def test_opus_enum(self):
        assert MODEL_MAP[ModelTier.OPUS] == "anthropic/claude-opus-4-6"


class TestThinkingMap:
    """THINKING_MAP must cover all levels."""

    def test_off_is_none(self):
        assert THINKING_MAP["off"] is None

    def test_low(self):
        assert THINKING_MAP["low"] == "low"

    def test_medium(self):
        assert THINKING_MAP["medium"] == "medium"

    def test_high(self):
        assert THINKING_MAP["high"] == "high"


# ---------------------------------------------------------------------------
# Constructor / init tests
# ---------------------------------------------------------------------------


class TestOpenClawExecutorInit:
    def test_default_gateway_url(self, monkeypatch):
        monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)
        monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
        ex = OpenClawExecutor()
        assert ex.gateway_url == "http://localhost:18789"

    def test_custom_gateway_url(self):
        ex = OpenClawExecutor(gateway_url="http://myhost:8888")
        assert ex.gateway_url == "http://myhost:8888"

    def test_trailing_slash_stripped(self):
        ex = OpenClawExecutor(gateway_url="http://myhost:8888/")
        assert ex.gateway_url == "http://myhost:8888"

    def test_env_gateway_url(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://envhost:5555")
        ex = OpenClawExecutor()
        assert ex.gateway_url == "http://envhost:5555"

    def test_env_gateway_token(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "env-token-123")
        ex = OpenClawExecutor()
        assert ex.gateway_token == "env-token-123"

    def test_explicit_token(self):
        ex = OpenClawExecutor(gateway_token="explicit-token")
        assert ex.gateway_token == "explicit-token"

    def test_can_handle_all_types(self, executor):
        for task_type in TaskType:
            assert executor.can_handle(task_type) is True

    def test_dry_run_flag_default_false(self, executor):
        assert executor.dry_run is False

    def test_dry_run_flag_set(self, dry_executor):
        assert dry_executor.dry_run is True


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_returns_success(self, dry_executor, sample_task):
        result = dry_executor.execute(sample_task, worker_id="test-worker")
        assert result.state == TaskState.SUCCESS

    def test_dry_run_contains_dry_run_flag(self, dry_executor, sample_task):
        result = dry_executor.execute(sample_task)
        assert result.result.get("dry_run") is True

    def test_dry_run_no_http(self, dry_executor, sample_task):
        with patch.object(dry_executor, "_http_post") as mock_post:
            dry_executor.execute(sample_task)
            mock_post.assert_not_called()

    def test_dry_run_model_tier_haiku(self, dry_executor, sample_task):
        result = dry_executor.execute(sample_task, model_tier="haiku")
        assert "haiku" in result.model_used

    def test_dry_run_model_tier_opus(self, dry_executor, sample_task):
        result = dry_executor.execute(sample_task, model_tier="opus")
        assert "opus" in result.model_used

    def test_dry_run_confidence(self, dry_executor, sample_task):
        result = dry_executor.execute(sample_task)
        assert result.confidence == 0.8

    def test_dry_run_has_text_output(self, dry_executor, sample_task):
        result = dry_executor.execute(sample_task)
        assert "text" in result.result
        assert len(result.result["text"]) > 0


# ---------------------------------------------------------------------------
# Model mapping via execute()
# ---------------------------------------------------------------------------


class TestModelTierMapping:
    """Test that model_tier strings/enums map to correct model strings."""

    def _run_with_mock(self, executor, task, model_tier):
        """Execute with mocked HTTP and return the spawned model."""
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": '{"status":"accepted","childSessionKey":"sess-001"}'}],
                "details": {"status": "accepted", "childSessionKey": "sess-001"},
            },
        }
        history_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": "sess-001",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": "test output"}], "stopReason": "stop"},
                    ],
                })}],
            },
        }

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return spawn_resp
            return history_resp

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(task, model_tier=model_tier)

        return result.model_used

    def test_haiku(self, executor, sample_task):
        model = self._run_with_mock(executor, sample_task, "haiku")
        assert model == "anthropic/claude-haiku-4-5-20251001"

    def test_sonnet(self, executor, sample_task):
        model = self._run_with_mock(executor, sample_task, "sonnet")
        assert model == "anthropic/claude-sonnet-4-6"

    def test_opus(self, executor, sample_task):
        model = self._run_with_mock(executor, sample_task, "opus")
        assert model == "anthropic/claude-opus-4-6"

    def test_default_to_sonnet_when_none(self, executor, sample_task):
        # task has no preferred_model set → should default to sonnet
        model = self._run_with_mock(executor, sample_task, None)
        assert model == "anthropic/claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Thinking level mapping
# ---------------------------------------------------------------------------


class TestThinkingLevelMapping:
    """Test that thinking_level is forwarded in the spawn request body."""

    def _capture_spawn_body(self, executor, task, thinking_level):
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": '{"status":"accepted","childSessionKey":"sess-002"}'}],
                "details": {"status": "accepted", "childSessionKey": "sess-002"},
            },
        }
        history_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": "sess-002",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": "ok"}], "stopReason": "stop"},
                    ],
                })}],
            },
        }

        captured_args = {}

        def fake_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured_args.update(body.get("args", {}))
                return spawn_resp
            return history_resp

        with patch.object(executor, "_http_post", side_effect=fake_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            executor.execute(task, thinking_level=thinking_level)

        return captured_args

    def test_off_not_in_body(self, executor, sample_task):
        body = self._capture_spawn_body(executor, sample_task, "off")
        # thinking=None means key should be absent
        assert body.get("thinking") is None or "thinking" not in body

    def test_low_in_body(self, executor, sample_task):
        body = self._capture_spawn_body(executor, sample_task, "low")
        assert body.get("thinking") == "low"

    def test_medium_in_body(self, executor, sample_task):
        body = self._capture_spawn_body(executor, sample_task, "medium")
        assert body.get("thinking") == "medium"

    def test_high_in_body(self, executor, sample_task):
        body = self._capture_spawn_body(executor, sample_task, "high")
        assert body.get("thinking") == "high"


# ---------------------------------------------------------------------------
# Successful execution flow
# ---------------------------------------------------------------------------


class TestSuccessfulExecution:

    def _make_mock_post(self, session_key, output_text, poll_rounds=0):
        """Build a mock _http_post that handles spawn + history polling."""
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"status": "accepted", "childSessionKey": session_key})}],
                "details": {"status": "accepted", "childSessionKey": session_key},
            },
        }
        running_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "prompt"}]}],
                })}],
            },
        }
        done_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": output_text}],
                         "stopReason": "stop", "usage": {"input": 100, "output": 50}},
                    ],
                })}],
            },
        }
        # sessions_list response for token extraction
        list_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessions": [
                        {"sessionKey": session_key, "totalTokens": 1500},
                    ],
                })}],
            },
        }
        call_count = {"history": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return spawn_resp
            if body.get("tool") == "sessions_list":
                return list_resp
            # sessions_history
            call_count["history"] += 1
            if call_count["history"] <= poll_rounds:
                return running_resp
            return done_resp

        return mock_post

    def test_returns_success_state(self, executor, sample_task):
        mock = self._make_mock_post("sess-003", "Hello, world!")

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS

    def test_output_text_extracted(self, executor, sample_task):
        mock = self._make_mock_post("sess-004", "Pipeline result text")

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.result["text"] == "Pipeline result text"

    def test_tokens_extracted_from_sessions_list(self, executor, sample_task):
        mock = self._make_mock_post("sess-tok", "token test output")

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.tokens_consumed == 1500

    def test_polls_until_done(self, executor, sample_task):
        mock = self._make_mock_post("sess-005", "final", poll_rounds=2)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert result.result["text"] == "final"

    def test_session_key_from_text_fallback(self, executor, sample_task):
        """Session key extracted from text content when details is missing."""
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": '{"status":"accepted","childSessionKey":"sess-006"}'}],
                # No details key
            },
        }
        done_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": "sess-006",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": "via text fallback"}],
                         "stopReason": "stop", "usage": {"input": 100, "output": 50}},
                    ],
                })}],
            },
        }

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return spawn_resp
            return done_resp

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_http_500_on_spawn_returns_failed(self, executor, sample_task):
        with patch.object(executor, "_http_post",
                          side_effect=RuntimeError("Gateway HTTP error 500: Internal Error")):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert len(result.errors) == 1
        assert "500" in result.errors[0].message

    def test_spawn_not_ok_returns_failed(self, executor, sample_task):
        spawn_resp = {"ok": False, "error": {"type": "tool_error", "message": "spawn failed"}}

        with patch.object(executor, "_http_post", return_value=spawn_resp):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED

    def test_empty_session_output_returns_failed(self, executor, sample_task):
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": '{"status":"accepted","childSessionKey":"sess-empty"}'}],
                "details": {"childSessionKey": "sess-empty"},
            },
        }
        # History shows assistant with empty text but stopReason=stop (completed)
        done_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": "sess-empty",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": ""}], "stopReason": "stop"},
                    ],
                })}],
            },
        }

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return spawn_resp
            return done_resp

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        # Empty output → FAILED
        assert result.state == TaskState.FAILED

    def test_missing_session_key_returns_failed(self, executor, sample_task):
        # Gateway returns ok but no childSessionKey
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": '{"status":"accepted"}'}],
                "details": {"status": "accepted"},
            },
        }

        with patch.object(executor, "_http_post", return_value=spawn_resp):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


class TestTimeoutHandling:
    def _make_running_mock(self, session_key):
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"status": "accepted", "childSessionKey": session_key})}],
                "details": {"childSessionKey": session_key},
            },
        }
        # Session never completes — no stopReason, only user message
        running_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "prompt"}]}],
                })}],
            },
        }

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return spawn_resp
            return running_resp

        return mock_post

    def test_timeout_returns_failed(self, executor, sample_task):
        executor.timeout_seconds = 1
        sample_task.timeout_seconds = 1
        mock = self._make_running_mock("sess-timeout")

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"), \
             patch("orchestration_engine.openclaw_executor.time.monotonic",
                   side_effect=[0.0, 0.0, 2.0]):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.errors[0].code == "timeout"

    def test_timeout_error_message_contains_session_key(self, executor, sample_task):
        executor.timeout_seconds = 1
        sample_task.timeout_seconds = 1
        mock = self._make_running_mock("my-session-xyz")

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"), \
             patch("orchestration_engine.openclaw_executor.time.monotonic",
                   side_effect=[0.0, 0.0, 2.0]):
            result = executor.execute(sample_task)

        assert "my-session-xyz" in result.errors[0].message


# ---------------------------------------------------------------------------
# Bearer token in headers
# ---------------------------------------------------------------------------


class TestAuthHeaders:
    def test_token_sent_in_authorization_header(self):
        ex = OpenClawExecutor(gateway_token="secret-bearer-token")
        headers = ex._build_headers()
        assert headers.get("Authorization") == "Bearer secret-bearer-token"

    def test_no_token_no_authorization_header(self):
        ex = OpenClawExecutor(gateway_token="")
        headers = ex._build_headers()
        assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# CLI wiring test
# ---------------------------------------------------------------------------


class TestCLIWiring:
    """Test that `orch run --mode openclaw` creates an OpenClawExecutor."""

    def test_openclaw_mode_creates_openclaw_executor(self, tmp_path):
        """--mode openclaw path calls PipelineRunner.openclaw() not .standalone()."""
        from orchestration_engine.cli import main
        import orchestration_engine.pipeline_runner as pr_module

        # Minimal hello-pipeline YAML for the test
        template_yaml = tmp_path / "test_pipe.yaml"
        template_yaml.write_text(
            """
id: test-pipe
name: Test Pipe
version: "1.0.0"
description: Minimal test pipeline
phases:
  - id: phase-one
    name: Phase One
    description: First phase
    task_type: content
    model_tier: sonnet
    thinking_level: "off"
    depends_on: []
    timeout_minutes: 1
    prompt_template: "Hello {input[topic]}"
    output_schema:
      type: object
      properties:
        result:
          type: string
""",
            encoding="utf-8",
        )

        runner = CliRunner()

        created_runners = []

        original_openclaw = pr_module.PipelineRunner.openclaw

        @classmethod
        def fake_openclaw(cls, gateway_url=None, gateway_token=None, **kwargs):
            from orchestration_engine.openclaw_executor import OpenClawExecutor as OCE
            # dry_run=True so no real HTTP is attempted
            executor = OCE(
                gateway_url=gateway_url or "http://localhost:18789",
                gateway_token=gateway_token,
                dry_run=True,
            )
            instance = pr_module.PipelineRunner.__new__(pr_module.PipelineRunner)
            from orchestration_engine.db import Database
            from orchestration_engine.queue import TaskQueue
            instance._db = Database(":memory:")
            instance._tmp_dir = None
            instance._db_path = ":memory:"
            instance.queue = TaskQueue(instance._db)
            instance.executors = [executor]
            created_runners.append(instance)
            return instance

        with patch.object(pr_module.PipelineRunner, "openclaw", fake_openclaw):
            result = runner.invoke(
                main,
                [
                    "run",
                    str(template_yaml),
                    "--mode", "openclaw",
                    "--input", '{"topic": "test"}',
                ],
                catch_exceptions=False,
            )

        assert len(created_runners) == 1, (
            f"PipelineRunner.openclaw() should have been called once. "
            f"Output: {result.output}"
        )
        from orchestration_engine.openclaw_executor import OpenClawExecutor as OCE
        assert isinstance(created_runners[0].executors[0], OCE)

    def test_gateway_url_option_passed_to_runner(self, tmp_path):
        """--gateway-url is forwarded to PipelineRunner.openclaw()."""
        from orchestration_engine.cli import main
        import orchestration_engine.pipeline_runner as pr_module

        template_yaml = tmp_path / "pipe.yaml"
        template_yaml.write_text(
            """
id: pipe2
name: Pipe2
version: "1.0.0"
description: ""
phases:
  - id: p1
    name: P1
    description: ""
    task_type: content
    model_tier: sonnet
    thinking_level: "off"
    depends_on: []
    timeout_minutes: 1
    prompt_template: "Hi"
    output_schema:
      type: object
      properties:
        result:
          type: string
""",
            encoding="utf-8",
        )

        runner = CliRunner()
        captured_kwargs = {}

        original_openclaw = pr_module.PipelineRunner.openclaw

        @classmethod
        def fake_openclaw(cls, gateway_url=None, gateway_token=None, **kwargs):
            captured_kwargs["gateway_url"] = gateway_url
            captured_kwargs["gateway_token"] = gateway_token
            from orchestration_engine.openclaw_executor import OpenClawExecutor as OCE
            executor = OCE(dry_run=True, gateway_url=gateway_url)
            instance = pr_module.PipelineRunner.__new__(pr_module.PipelineRunner)
            from orchestration_engine.db import Database
            from orchestration_engine.queue import TaskQueue
            instance._db = Database(":memory:")
            instance._tmp_dir = None
            instance._db_path = ":memory:"
            instance.queue = TaskQueue(instance._db)
            instance.executors = [executor]
            return instance

        with patch.object(pr_module.PipelineRunner, "openclaw", fake_openclaw):
            runner.invoke(
                main,
                [
                    "run",
                    str(template_yaml),
                    "--mode", "openclaw",
                    "--gateway-url", "http://custom-host:9999",
                    "--gateway-token", "my-secret-token",
                    "--input", '{"topic": "test"}',
                ],
                catch_exceptions=False,
            )

        assert captured_kwargs.get("gateway_url") == "http://custom-host:9999"
        assert captured_kwargs.get("gateway_token") == "my-secret-token"
