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


# ---------------------------------------------------------------------------
# Issue #210 — Output capture tests
# ---------------------------------------------------------------------------


class TestOutputCaptureInstruction:
    """The OUTPUT_CAPTURE_INSTRUCTION must exist and be appended to prompts."""

    def test_constant_exists_and_non_empty(self):
        from orchestration_engine.openclaw_executor import OUTPUT_CAPTURE_INSTRUCTION

        assert isinstance(OUTPUT_CAPTURE_INSTRUCTION, str)
        assert len(OUTPUT_CAPTURE_INSTRUCTION) > 0

    def test_constant_contains_key_guidance(self):
        from orchestration_engine.openclaw_executor import OUTPUT_CAPTURE_INSTRUCTION

        # Must tell the sub-agent to return output as text
        assert "COMPLETE output" in OUTPUT_CAPTURE_INSTRUCTION or "complete output" in OUTPUT_CAPTURE_INSTRUCTION.lower()
        # Must warn against writing to files
        assert "file" in OUTPUT_CAPTURE_INSTRUCTION.lower()

    def test_instruction_appended_to_prompt(self, executor, sample_task):
        """The spawn payload must include the capture instruction in the task text."""
        from orchestration_engine.openclaw_executor import OUTPUT_CAPTURE_INSTRUCTION

        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": '{"status":"accepted","childSessionKey":"sess-210a"}'}],
                "details": {"childSessionKey": "sess-210a"},
            },
        }
        done_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": "sess-210a",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": "done"}],
                         "stopReason": "stop"},
                    ],
                })}],
            },
        }

        captured_task_texts = []

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured_task_texts.append(body.get("args", {}).get("task", ""))
                return spawn_resp
            return done_resp

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            executor.execute(sample_task)

        assert captured_task_texts, "sessions_spawn was never called"
        task_text = captured_task_texts[0]
        assert OUTPUT_CAPTURE_INSTRUCTION in task_text, (
            f"OUTPUT_CAPTURE_INSTRUCTION not found in spawn payload. "
            f"Payload: {task_text[:200]!r}"
        )

    def test_instruction_appended_to_non_empty_prompt(self, executor):
        """When task has a prompt, instruction is appended after it."""
        from orchestration_engine.openclaw_executor import OUTPUT_CAPTURE_INSTRUCTION

        task = TaskSpec(
            type=TaskType.CONTENT,
            payload={"prompt": "Write a summary of the following article: ..."},
            priority=Priority.NORMAL,
        )

        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": '{"childSessionKey":"sess-210b"}'}],
                "details": {"childSessionKey": "sess-210b"},
            },
        }
        done_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": "sess-210b",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "x"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": "result"}],
                         "stopReason": "stop"},
                    ],
                })}],
            },
        }

        captured = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured["task"] = body.get("args", {}).get("task", "")
                return spawn_resp
            return done_resp

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            executor.execute(task)

        task_text = captured.get("task", "")
        # Original prompt must still be present
        assert "Write a summary" in task_text
        # Instruction must follow it
        assert task_text.index("Write a summary") < task_text.index(OUTPUT_CAPTURE_INSTRUCTION)

    def test_dry_run_still_works_with_instruction(self, dry_executor, sample_task):
        """Dry-run mode must not break when instruction is appended."""
        result = dry_executor.execute(sample_task)
        assert result.state == TaskState.SUCCESS
        assert result.result.get("dry_run") is True


class TestFullTranscriptCapture:
    """After fix for #210, _run_session collects text from ALL assistant messages."""

    def _make_multi_turn_mock(self, session_key, messages):
        """Helper: build mock that returns a multi-turn conversation."""
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"childSessionKey": session_key})}],
                "details": {"childSessionKey": session_key},
            },
        }
        done_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": messages,
                })}],
            },
        }

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return spawn_resp
            # sessions_list token query
            if body.get("tool") == "sessions_list":
                return {
                    "ok": True,
                    "result": {"content": [{"type": "text", "text": json.dumps({"sessions": []})}]},
                }
            return done_resp

        return mock_post

    def test_all_assistant_messages_collected(self, executor, sample_task):
        """Text from every assistant turn is included in the output."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "do the thing"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "PART ONE: analysis here"}]},
            {"role": "user", "content": [{"type": "text", "text": "continue"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "PART TWO: implementation here"}],
             "stopReason": "stop"},
        ]

        mock = self._make_multi_turn_mock("sess-multi", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        output = result.result["text"]
        assert "PART ONE: analysis here" in output, "First assistant message missing from output"
        assert "PART TWO: implementation here" in output, "Last assistant message missing from output"

    def test_final_summary_only_session_still_works(self, executor, sample_task):
        """Single-turn session (original pattern) still captured correctly."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Single complete output"}],
             "stopReason": "stop"},
        ]

        mock = self._make_multi_turn_mock("sess-single", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert result.result["text"] == "Single complete output"

    def test_tool_using_session_captures_text_across_turns(self, executor, sample_task):
        """Session that uses tools between text messages — all text parts captured."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "research this"}]},
            # First assistant turn: thinking + tool call (tool_use block, no text)
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "web_search", "id": "tu1", "input": {"query": "topic"}},
            ]},
            # Tool result
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": "search results here"},
            ]},
            # Second assistant turn: substantive output
            {"role": "assistant", "content": [
                {"type": "text", "text": "RESEARCH FINDINGS: detailed 20KB analysis..."},
                {"type": "tool_use", "name": "write", "id": "tu2",
                 "input": {"path": "/tmp/out.md", "content": "written content"}},
            ]},
            # Tool result for write
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu2", "content": "file written"},
            ]},
            # Final brief summary
            {"role": "assistant", "content": [
                {"type": "text", "text": "Done. Output saved."},
            ], "stopReason": "stop"},
        ]

        mock = self._make_multi_turn_mock("sess-tools", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        output = result.result["text"]
        # Must include the substantive research text, not just the brief "Done."
        assert "RESEARCH FINDINGS" in output, "Substantive research text not in output"
        # Must also include the final summary
        assert "Done. Output saved." in output
        # Must NOT include the tool_use JSON or tool_result text
        assert "search results here" not in output

    def test_output_is_not_empty_when_first_turn_has_text(self, executor, sample_task):
        """Regression: output is non-empty even if final assistant msg is tool-only."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
            # First turn has text
            {"role": "assistant", "content": [{"type": "text", "text": "Here is the output: big content"}]},
            # Tool use
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
            # Final turn: only a tool call, no text
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "write", "id": "x", "input": {}},
            ], "stopReason": "stop"},
        ]

        mock = self._make_multi_turn_mock("sess-tool-final", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        # With the fix, we collect text from ALL messages → non-empty
        assert result.state == TaskState.SUCCESS
        assert "big content" in result.result["text"]


class TestStopReasonErrorDetection:
    """#212: Executor must detect stopReason='error' as terminal and fail the phase."""

    def _make_error_mock(self, session_key, messages):
        """Helper: build mock that returns a session ending with stopReason=error."""
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"childSessionKey": session_key})}],
                "details": {"childSessionKey": session_key},
            },
        }
        done_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": messages,
                })}],
            },
        }

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return spawn_resp
            if body.get("tool") == "sessions_list":
                return {
                    "ok": True,
                    "result": {"content": [{"type": "text", "text": json.dumps({"sessions": []})}]},
                }
            return done_resp

        return mock_post

    def test_error_stop_reason_marks_phase_failed(self, executor, sample_task):
        """stopReason=error should result in TaskState.FAILED."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "do work"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Partial analysis..."}]},
            {"role": "assistant", "content": [], "stopReason": "error"},
        ]
        mock = self._make_error_mock("sess-err", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert any("stopReason='error'" in e.message for e in result.errors)

    def test_error_preserves_partial_output(self, executor, sample_task):
        """Partial output from before the error should be captured in result."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "research"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "SUBSTANTIAL RESEARCH OUTPUT HERE"},
            ]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "More findings..."},
            ], "stopReason": "error"},
        ]
        mock = self._make_error_mock("sess-err-partial", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert "SUBSTANTIAL RESEARCH OUTPUT" in result.result.get("partial_output", "")

    def test_error_with_no_output_still_fails_cleanly(self, executor, sample_task):
        """stopReason=error with empty content should fail without crash."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "do work"}]},
            {"role": "assistant", "content": [], "stopReason": "error"},
        ]
        mock = self._make_error_mock("sess-err-empty", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED

    def test_normal_stop_still_succeeds(self, executor, sample_task):
        """Regression: stopReason=stop should still return SUCCESS."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "do work"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Complete output"}],
             "stopReason": "stop"},
        ]
        mock = self._make_error_mock("sess-ok", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert result.result["text"] == "Complete output"

    def test_max_tokens_detected_as_terminal(self, executor, sample_task):
        """stopReason=max_tokens should be detected as terminal and fail."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "write a novel"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Chapter 1: It was a dark and stormy night..."},
            ], "stopReason": "max_tokens"},
        ]
        mock = self._make_error_mock("sess-maxtoken", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert "Chapter 1" in result.result.get("partial_output", "")


# ---------------------------------------------------------------------------
# Issue #240 — Poll Timeout
# ---------------------------------------------------------------------------


class TestPollTimeout:
    """Tests for the deadline-based poll timeout and 80% warning (#240)."""

    # ── helpers ──────────────────────────────────────────────────────────

    def _spawn_resp(self, session_key: str) -> dict:
        return {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"childSessionKey": session_key})}],
                "details": {"childSessionKey": session_key},
            },
        }

    def _running_resp(self, session_key: str) -> dict:
        """A response with only a user message — no terminal stopReason."""
        return {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
                    ],
                })}],
            },
        }

    def _done_resp(self, session_key: str, output: str = "done") -> dict:
        """A response with a terminal assistant message."""
        return {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": output}],
                         "stopReason": "stop"},
                    ],
                })}],
            },
        }

    # ── AC-1: None timeout → DEFAULT_TIMEOUT_SECONDS = 1200 ──────────────

    def test_none_timeout_uses_default_1200(self, executor, sample_task):
        """When effective_timeout resolves to None, the session must use 1200s (#240 AC-1)."""
        from orchestration_engine.openclaw_executor import DEFAULT_TIMEOUT_SECONDS

        assert DEFAULT_TIMEOUT_SECONDS == 1200, (
            f"DEFAULT_TIMEOUT_SECONDS must be 1200 (20 min), got {DEFAULT_TIMEOUT_SECONDS}"
        )

        captured_spawn_args: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured_spawn_args.update(body.get("args", {}))
                return self._spawn_resp("sess-timeout-none")
            if body.get("tool") == "sessions_list":
                return {"ok": True, "result": {"content": [{"type": "text", "text": "[]"}]}}
            return self._done_resp("sess-timeout-none")

        # Pass timeout=None directly to _run_session
        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            # Call _run_session with timeout=None
            executor._run_session("hello", "anthropic/claude-sonnet-4-6", None, timeout=None)

        assert captured_spawn_args.get("runTimeoutSeconds") == 1200, (
            f"Expected runTimeoutSeconds=1200, got {captured_spawn_args.get('runTimeoutSeconds')}"
        )

    # ── AC-2: Large timeout → used as-is ─────────────────────────────────

    def test_large_timeout_respected(self, executor, sample_task):
        """A timeout larger than 1200 must be used unchanged (#240 AC-2)."""
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return self._spawn_resp("sess-large-to")
            if body.get("tool") == "sessions_list":
                return {"ok": True, "result": {"content": [{"type": "text", "text": "[]"}]}}
            return self._done_resp("sess-large-to")

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            executor._run_session("hello", "anthropic/claude-sonnet-4-6", None, timeout=3600)

        assert captured.get("runTimeoutSeconds") == 3600

    # ── AC-3: Deadline exceeded → TimeoutError ────────────────────────────

    def test_deadline_exceeded_raises_timeout_error(self, executor, sample_task):
        """When monotonic() > deadline, TimeoutError must be raised (#240 AC-3)."""
        session_key = "sess-ac3"
        # monotonic call sequence:
        #   call 1 → loop_start (inside _run_session before loop)
        #   call 2 → first "now" in loop body → 0.0 (within deadline)
        #   call 3 → second "now" in loop body → 9999.0 (exceeds deadline)
        # The first call (loop_start=0.0), deadline = 0.0 + effective_timeout
        # Second iteration now=9999 → timeout

        mono_values = iter([
            0.0,    # loop_start
            0.0,    # first iteration now (within deadline — no timeout)
            9999.0, # second iteration now (exceeds deadline)
        ])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return self._spawn_resp(session_key)
            return self._running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"), \
             patch("orchestration_engine.openclaw_executor.time.monotonic",
                   side_effect=mono_values):
            with pytest.raises(TimeoutError) as exc_info:
                executor._run_session("hello", "anthropic/claude-sonnet-4-6", None, timeout=100)

        assert session_key in str(exc_info.value)

    # ── AC-4: 80% warning fires exactly once ─────────────────────────────

    def test_80_percent_warning_fires_once(self, executor, sample_task):
        """Warning logged at 80% elapsed, never repeated (#240 AC-4)."""
        session_key = "sess-ac4"
        timeout = 100  # seconds
        threshold = 0.8 * timeout  # 80s

        # Loop: start=0, iter1=below threshold, iter2=at/above threshold,
        #       iter3=still above, iter4=deadline exceeded → TimeoutError
        mono_values = iter([
            0.0,               # loop_start
            0.0,               # iter 1 now → below threshold (0 < 80)
            threshold + 1.0,   # iter 2 now → above threshold (81 > 80) → warning
            threshold + 2.0,   # iter 3 now → still above (should NOT warn again)
            9999.0,            # iter 4 now → deadline exceeded
        ])

        call_count = {"history": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return self._spawn_resp(session_key)
            # Always return running (no terminal reason) so loop keeps going
            return self._running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"), \
             patch("orchestration_engine.openclaw_executor.time.monotonic",
                   side_effect=mono_values), \
             patch("orchestration_engine.openclaw_executor.logger") as mock_logger:

            with pytest.raises(TimeoutError):
                executor._run_session("hello", "anthropic/claude-sonnet-4-6", None, timeout=timeout)

        # logger.warning must have been called exactly once with 80%+ info
        warning_calls = [
            c for c in mock_logger.warning.call_args_list
            if "elapsed" in str(c).lower() or "%" in str(c)
        ]
        assert len(warning_calls) == 1, (
            f"Expected exactly 1 80%-warning log call, got {len(warning_calls)}. "
            f"All warning calls: {mock_logger.warning.call_args_list}"
        )

    # ── AC-5: Multiple non-terminal responses eventually timeout ──────────

    def test_multiple_non_terminal_responses_eventually_timeout(self, executor, sample_task):
        """Loop fires TimeoutError even after many non-terminal gateway responses (#240 AC-5)."""
        session_key = "sess-ac5"
        # Simulate 3 non-terminal responses, then deadline exceeded
        # Monotonic: start=0, iter1=1, iter2=2, iter3=3, iter4=9999 (timeout)
        mono_values = iter([0.0, 1.0, 2.0, 3.0, 9999.0])
        poll_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return self._spawn_resp(session_key)
            poll_count["n"] += 1
            # Always return running — never completes
            return self._running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"), \
             patch("orchestration_engine.openclaw_executor.time.monotonic",
                   side_effect=mono_values):
            with pytest.raises(TimeoutError):
                executor._run_session("hello", "anthropic/claude-sonnet-4-6", None, timeout=10)

        # Must have polled at least twice before timing out
        assert poll_count["n"] >= 2, "Expected multiple polls before TimeoutError"


# ---------------------------------------------------------------------------
# Issue #241 — Session Cleanup Detection
# ---------------------------------------------------------------------------


class TestSessionCleanupDetection:
    """Tests for had_messages / gateway session GC detection (#241)."""

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _spawn_resp(session_key: str) -> dict:
        return {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"childSessionKey": session_key})}],
                "details": {"childSessionKey": session_key},
            },
        }

    @staticmethod
    def _make_poll_sequence(session_key: str, responses: list) -> callable:
        """Return a mock_post that yields history responses in order."""
        iter_responses = iter(responses)

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return TestSessionCleanupDetection._spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return {"ok": True, "result": {"content": [{"type": "text", "text": "[]"}]}}
            # sessions_history — return next in sequence
            try:
                hist_payload = next(iter_responses)
            except StopIteration:
                raise RuntimeError("Mock exhausted: no more history responses")
            return {
                "ok": True,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({
                        "sessionKey": session_key,
                        "messages": hist_payload,
                    })}],
                },
            }

        return mock_post

    # ── AC-6: had_messages starts False, first empty poll → no error ──────

    def test_first_poll_empty_messages_no_error(self, executor):
        """Empty messages on first poll must NOT raise — session not yet started (#241 AC-6, AC-10)."""
        session_key = "sess-241-ac6"
        task = TaskSpec(
            type=TaskType.CONTENT,
            payload={"prompt": "go"},
            priority=Priority.NORMAL,
        )

        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "final output"}],
             "stopReason": "stop"},
        ]

        # Sequence: empty, empty, done
        mock = self._make_poll_sequence(session_key, [
            [],            # poll 1 → empty (not yet started)
            [],            # poll 2 → still empty
            done_messages, # poll 3 → complete
        ])

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            output, _ = executor._run_session("go", "anthropic/claude-sonnet-4-6", None, timeout=60)

        assert "final output" in output

    # ── AC-7: had_messages set to True on first non-empty poll ───────────

    def test_had_messages_set_true_on_non_empty_poll(self, executor):
        """After a non-empty poll, had_messages is implicitly True — subsequent empty raises (#241 AC-7, AC-8)."""
        session_key = "sess-241-ac7"
        task = TaskSpec(
            type=TaskType.CONTENT,
            payload={"prompt": "go"},
            priority=Priority.NORMAL,
        )

        non_empty = [{"role": "user", "content": [{"type": "text", "text": "prompt"}]}]

        # Sequence: non-empty (sets had_messages=True), then empty (should raise)
        mock = self._make_poll_sequence(session_key, [
            non_empty,  # poll 1 → messages present → had_messages = True
            [],         # poll 2 → empty → RuntimeError
        ])

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            with pytest.raises(RuntimeError):
                executor._run_session("go", "anthropic/claude-sonnet-4-6", None, timeout=60)

    # ── AC-8: [non-empty, empty] → RuntimeError on second poll ───────────

    def test_gc_detected_on_second_poll(self, executor):
        """RuntimeError raised immediately when had_messages=True and poll returns empty (#241 AC-8)."""
        session_key = "sess-241-ac8"

        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]

        mock = self._make_poll_sequence(session_key, [
            non_empty,  # poll 1 → non-empty (had_messages → True)
            [],         # poll 2 → empty → RuntimeError
        ])

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            with pytest.raises(RuntimeError) as exc_info:
                executor._run_session("go", "anthropic/claude-sonnet-4-6", None, timeout=60)

        assert "garbage-collected" in str(exc_info.value).lower() or \
               "evicted" in str(exc_info.value).lower() or \
               session_key in str(exc_info.value), (
            f"Expected cleanup-related error message, got: {exc_info.value}"
        )

    # ── AC-9: RuntimeError message includes session_key ──────────────────

    def test_gc_error_includes_session_key(self, executor):
        """RuntimeError from GC detection must include the session key (#241 AC-9)."""
        session_key = "my-unique-session-key-xyz"

        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]

        mock = self._make_poll_sequence(session_key, [
            non_empty,
            [],
        ])

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            with pytest.raises(RuntimeError) as exc_info:
                executor._run_session("go", "anthropic/claude-sonnet-4-6", None, timeout=60)

        assert session_key in str(exc_info.value), (
            f"Session key '{session_key}' not found in error: {exc_info.value}"
        )

    # ── AC-10: [empty, empty, non-empty] → no error ──────────────────────

    def test_multiple_empty_polls_before_start_no_error(self, executor):
        """Several empty polls before session starts must NOT raise (#241 AC-10)."""
        session_key = "sess-241-ac10"

        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "result text"}],
             "stopReason": "stop"},
        ]

        mock = self._make_poll_sequence(session_key, [
            [],             # poll 1 → empty
            [],             # poll 2 → empty
            [],             # poll 3 → empty
            done_messages,  # poll 4 → done
        ])

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            output, _ = executor._run_session("go", "anthropic/claude-sonnet-4-6", None, timeout=120)

        assert "result text" in output

    # ── Edge: None messages treated as empty (defensive) ─────────────────

    def test_none_messages_treated_as_empty(self, executor):
        """History returning messages=None must be treated as empty, not crash (#241 edge)."""
        session_key = "sess-241-none"

        # First poll returns {"messages": null} — defensive: must not crash
        null_messages_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": None,
                })}],
            },
        }
        done_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "x"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                         "stopReason": "stop"},
                    ],
                })}],
            },
        }
        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return TestSessionCleanupDetection._spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return {"ok": True, "result": {"content": [{"type": "text", "text": "[]"}]}}
            call_count["n"] += 1
            if call_count["n"] == 1:
                return null_messages_resp
            return done_resp

        with patch.object(executor, "_http_post", side_effect=mock_post), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            output, _ = executor._run_session("go", "anthropic/claude-sonnet-4-6", None, timeout=60)

        assert "ok" in output

    # ── GC detection propagates as RuntimeError → FAILED TaskResult ───────

    def test_gc_detection_causes_failed_task_result(self, executor, sample_task):
        """RuntimeError from GC detection must be caught and returned as TaskState.FAILED."""
        session_key = "sess-gc-task"

        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]

        mock = self._make_poll_sequence(session_key, [
            non_empty,
            [],
        ])

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            # Use execute() so the RuntimeError is caught and wrapped in TaskResult
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.errors, "TaskResult should have at least one error"
        error_message = result.errors[0].message
        # The error message should reference the session GC or the session key
        assert session_key in error_message or "garbage" in error_message.lower() or \
               "evicted" in error_message.lower(), (
            f"Expected GC-related message in error, got: {error_message!r}"
        )


# ---------------------------------------------------------------------------
# Issue #239 — sessions_history limit raised to capture full sub-agent output
# ---------------------------------------------------------------------------


class TestSessionsHistoryLimit:
    """Issue #239 — sessions_history must use SESSIONS_HISTORY_LIMIT (≥ 1000), not 200."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_mock(self, session_key: str, messages: list, *, capture_limits=None):
        """Build a mock _http_post that records the 'limit' arg sent to sessions_history.

        Args:
            session_key:    The session key to embed in spawn/history responses.
            messages:       The message list to return from sessions_history.
            capture_limits: If a list is provided, each 'limit' value sent to
                            sessions_history is appended to it.
        """
        spawn_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"childSessionKey": session_key})}],
                "details": {"childSessionKey": session_key},
            },
        }
        history_resp = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": messages,
                })}],
            },
        }
        list_resp = {
            "ok": True,
            "result": {"content": [{"type": "text", "text": json.dumps({"sessions": []})}]},
        }

        def mock_post(url, body):
            tool = body.get("tool", "")
            if tool == "sessions_spawn":
                return spawn_resp
            if tool == "sessions_list":
                return list_resp
            if tool == "sessions_history":
                if capture_limits is not None:
                    capture_limits.append(body.get("args", {}).get("limit"))
                return history_resp
            return {"ok": True, "result": {"content": [{"type": "text", "text": "{}"}]}}

        return mock_post

    def _make_messages(self, n_assistant: int) -> list:
        """Build a realistic messages list with n_assistant assistant turns.

        The first message is a user prompt.  Each assistant turn contains
        unique text ``chunk_<i>`` to allow individual verification.
        The final assistant message carries ``stopReason='stop'`` so the
        polling loop recognises session completion.
        """
        msgs = [{"role": "user", "content": [{"type": "text", "text": "do extensive research"}]}]
        for i in range(n_assistant):
            is_last = i == n_assistant - 1
            entry: dict = {
                "role": "assistant",
                "content": [{"type": "text", "text": f"chunk_{i}"}],
            }
            if is_last:
                entry["stopReason"] = "stop"
            msgs.append(entry)
        return msgs

    # ------------------------------------------------------------------
    # Constant existence / value
    # ------------------------------------------------------------------

    def test_limit_constant_exists_and_is_int(self):
        """SESSIONS_HISTORY_LIMIT must be importable and be an integer."""
        from orchestration_engine.openclaw_executor import SESSIONS_HISTORY_LIMIT

        assert isinstance(SESSIONS_HISTORY_LIMIT, int), (
            f"Expected int, got {type(SESSIONS_HISTORY_LIMIT)}"
        )

    def test_limit_constant_is_at_least_1000(self):
        """SESSIONS_HISTORY_LIMIT must be ≥ 1000 (well above the old 200 ceiling)."""
        from orchestration_engine.openclaw_executor import SESSIONS_HISTORY_LIMIT

        assert SESSIONS_HISTORY_LIMIT >= 1000, (
            f"SESSIONS_HISTORY_LIMIT={SESSIONS_HISTORY_LIMIT} is below required minimum of 1000"
        )

    def test_hardcoded_200_not_in_source(self):
        """Regression guard: the old hardcoded limit=200 must be gone."""
        import ast
        import pathlib

        src = pathlib.Path(
            __file__
        ).parent.parent / "src" / "orchestration_engine" / "openclaw_executor.py"
        tree = ast.parse(src.read_text())

        for node in ast.walk(tree):
            # Look for dict literals with key="limit" and value=200
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "limit"
                        and isinstance(value, ast.Constant)
                        and value.value == 200
                    ):
                        pytest.fail(
                            "Found hardcoded limit=200 in a dict literal in "
                            "openclaw_executor.py — this should use SESSIONS_HISTORY_LIMIT"
                        )

    # ------------------------------------------------------------------
    # Limit forwarded in HTTP call
    # ------------------------------------------------------------------

    def test_sessions_history_called_with_limit_constant(self, executor, sample_task):
        """The SESSIONS_HISTORY_LIMIT value must be forwarded to the gateway."""
        from orchestration_engine.openclaw_executor import SESSIONS_HISTORY_LIMIT

        captured_limits: list = []
        messages = self._make_messages(5)
        mock = self._make_mock("sess-lim-fwd", messages, capture_limits=captured_limits)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            executor.execute(sample_task)

        assert captured_limits, "sessions_history was never called"
        assert all(lim == SESSIONS_HISTORY_LIMIT for lim in captured_limits), (
            f"Expected every sessions_history call to use limit={SESSIONS_HISTORY_LIMIT}, "
            f"but got limits: {captured_limits}"
        )

    def test_sessions_history_not_called_with_200(self, executor, sample_task):
        """Regression: sessions_history must never be called with limit=200."""
        captured_limits: list = []
        messages = self._make_messages(5)
        mock = self._make_mock("sess-no-200", messages, capture_limits=captured_limits)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            executor.execute(sample_task)

        assert 200 not in captured_limits, (
            "sessions_history was called with the old hardcoded limit=200; "
            "it must use SESSIONS_HISTORY_LIMIT instead"
        )

    # ------------------------------------------------------------------
    # AC-4 — 150+ assistant messages fully captured
    # ------------------------------------------------------------------

    def test_sessions_history_captures_all_messages_beyond_original_limit(
        self, executor, sample_task
    ):
        """A session with 250 assistant messages must have ALL chunks in the output.

        This is the primary regression test for issue #239.  With the old
        limit=200, messages 0–49 would be missing from the response when the
        gateway returns only the last 200.  With limit=SESSIONS_HISTORY_LIMIT
        (1000), all 250 are requested and the output must contain every chunk.

        The mock does NOT simulate gateway-level truncation: it always returns
        the full 250-message list.  The test therefore verifies two invariants:
          1. The code sends a high enough limit to request all messages.
          2. The extraction loop collects text from every assistant message.
        """
        N = 250  # well above the old limit of 200

        messages = self._make_messages(N)
        mock = self._make_mock("sess-250-chunks", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS, (
            f"Expected SUCCESS but got {result.state}; errors={result.errors}"
        )

        output_text = result.result.get("text", "")

        missing = [f"chunk_{i}" for i in range(N) if f"chunk_{i}" not in output_text]
        assert not missing, (
            f"{len(missing)} / {N} chunks are missing from the output. "
            f"First 10 missing: {missing[:10]!r}"
        )

    def test_exactly_150_assistant_messages_all_captured(self, executor, sample_task):
        """Acceptance-criteria variant: exactly 150 assistant messages, all captured."""
        N = 150

        messages = self._make_messages(N)
        mock = self._make_mock("sess-150-chunks", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        output_text = result.result.get("text", "")
        for i in range(N):
            assert f"chunk_{i}" in output_text, (
                f"chunk_{i} is missing from the output. "
                f"Output excerpt: {output_text[:200]!r}"
            )

    # ------------------------------------------------------------------
    # E-1 — Boundary warning when response is at limit ceiling
    # ------------------------------------------------------------------

    def test_warning_emitted_when_messages_equals_limit(self, executor, sample_task, caplog):
        """A logger.warning must be emitted when len(messages) == SESSIONS_HISTORY_LIMIT.

        This indicates the response may be truncated (gateway hit the ceiling).
        """
        import logging
        from orchestration_engine.openclaw_executor import SESSIONS_HISTORY_LIMIT

        # Build exactly SESSIONS_HISTORY_LIMIT messages: 1 user + (limit-1) assistant
        # assistant messages + 1 final with stopReason, totalling exactly limit.
        n_assistant = SESSIONS_HISTORY_LIMIT - 1  # last message is the user msg + n assistant
        # Actually: 1 user + (SESSIONS_HISTORY_LIMIT-1) assistant = SESSIONS_HISTORY_LIMIT total
        msgs = [{"role": "user", "content": [{"type": "text", "text": "prompt"}]}]
        for i in range(n_assistant):
            is_last = i == n_assistant - 1
            entry: dict = {
                "role": "assistant",
                "content": [{"type": "text", "text": f"chunk_{i}"}],
            }
            if is_last:
                entry["stopReason"] = "stop"
            msgs.append(entry)

        assert len(msgs) == SESSIONS_HISTORY_LIMIT, (
            f"Test setup error: expected {SESSIONS_HISTORY_LIMIT} messages, got {len(msgs)}"
        )

        mock = self._make_mock("sess-at-limit", msgs)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"), \
             caplog.at_level(logging.WARNING, logger="orchestration_engine.openclaw_executor"):
            executor.execute(sample_task)

        # At least one WARNING record should mention the ceiling
        limit_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and str(SESSIONS_HISTORY_LIMIT) in r.getMessage()
        ]
        assert limit_warnings, (
            "Expected a logger.warning mentioning SESSIONS_HISTORY_LIMIT when "
            "response length equals the limit, but none was found. "
            f"All log records: {[(r.levelno, r.getMessage()) for r in caplog.records]}"
        )

    def test_no_warning_when_messages_below_limit(self, executor, sample_task, caplog):
        """No truncation warning should appear for ordinary short sessions."""
        import logging
        from orchestration_engine.openclaw_executor import SESSIONS_HISTORY_LIMIT

        # A normal small session — way below the limit
        messages = self._make_messages(10)
        mock = self._make_mock("sess-small", messages)

        with patch.object(executor, "_http_post", side_effect=mock), \
             patch("orchestration_engine.openclaw_executor.time.sleep"), \
             caplog.at_level(logging.WARNING, logger="orchestration_engine.openclaw_executor"):
            executor.execute(sample_task)

        # No warning about hitting the limit ceiling should be present
        limit_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "limit" in r.getMessage().lower()
            and str(SESSIONS_HISTORY_LIMIT) in r.getMessage()
        ]
        assert not limit_warnings, (
            f"Unexpected limit warning for a short session: "
            f"{[r.getMessage() for r in limit_warnings]}"
        )
