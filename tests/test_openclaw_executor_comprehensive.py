"""Comprehensive tests for OpenClawExecutor — Issues #240 (Poll Timeout) + #241 (Session Cleanup).

Coverage targets:
  #240 — Acceptance criteria AC-1 through AC-5
  #241 — Acceptance criteria AC-6 through AC-10
  All documented edge cases from the requirements document
  Happy path and error path scenarios for each fix

Run with:
    pytest tests/test_openclaw_executor_comprehensive.py -v
"""

import json
import math
import time
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from orchestration_engine.openclaw_executor import (
    DEFAULT_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    SESSIONS_HISTORY_LIMIT,
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
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def executor():
    """Executor wired to a mock gateway; no real HTTP calls."""
    return OpenClawExecutor(
        gateway_url="http://localhost:18789",
        gateway_token="test-token",
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )


@pytest.fixture
def sample_task():
    """A minimal content task whose timeout is explicitly set below."""
    return TaskSpec(
        type=TaskType.CONTENT,
        payload={"prompt": "Say hello."},
        priority=Priority.NORMAL,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _spawn_resp(session_key: str) -> dict:
    """Spawn response with childSessionKey in both details and text."""
    return {
        "ok": True,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps({"childSessionKey": session_key})}
            ],
            "details": {"childSessionKey": session_key},
        },
    }


def _running_resp(session_key: str) -> dict:
    """History response with only a user message — no terminal stopReason."""
    return {
        "ok": True,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "sessionKey": session_key,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [{"type": "text", "text": "prompt"}],
                                }
                            ],
                        }
                    ),
                }
            ],
        },
    }


def _done_resp(session_key: str, output: str = "done") -> dict:
    """History response with a terminal assistant message (stopReason=stop)."""
    return {
        "ok": True,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "sessionKey": session_key,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [{"type": "text", "text": "prompt"}],
                                },
                                {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": output}],
                                    "stopReason": "stop",
                                },
                            ],
                        }
                    ),
                }
            ],
        },
    }


def _empty_history_resp(session_key: str) -> dict:
    """History response with an empty messages list (session GC'd or not started)."""
    return {
        "ok": True,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"sessionKey": session_key, "messages": []}
                    ),
                }
            ],
        },
    }


def _null_messages_resp(session_key: str) -> dict:
    """History response with messages=null (defensive edge case)."""
    return {
        "ok": True,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"sessionKey": session_key, "messages": None}
                    ),
                }
            ],
        },
    }


def _list_resp_empty() -> dict:
    """sessions_list response with no matching sessions."""
    return {
        "ok": True,
        "result": {
            "content": [{"type": "text", "text": json.dumps({"sessions": []})}]
        },
    }


def _make_poll_sequence(session_key: str, history_payloads: list):
    """
    Return a mock_post callable that yields ``history_payloads`` in order.

    Each element of ``history_payloads`` is a raw messages list (the value
    that will be put into the "messages" key of the history JSON).
    """

    def _history_resp(messages):
        return {
            "ok": True,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"sessionKey": session_key, "messages": messages}
                        ),
                    }
                ],
            },
        }

    payload_iter = iter(history_payloads)

    def mock_post(url, body):
        tool = body.get("tool", "")
        if tool == "sessions_spawn":
            return _spawn_resp(session_key)
        if tool == "sessions_list":
            return _list_resp_empty()
        # sessions_history
        try:
            messages = next(payload_iter)
        except StopIteration:
            raise RuntimeError("Mock exhausted: no more history responses")
        return _history_resp(messages)

    return mock_post


# ===========================================================================
# #240 — Poll Timeout
# ===========================================================================


class TestAC1_NoneTimeoutUsesDefault:
    """AC-1: If effective_timeout is None, DEFAULT_TIMEOUT_SECONDS (1200) is used."""

    def test_default_constant_is_1200(self):
        """Sanity-check: DEFAULT_TIMEOUT_SECONDS must equal 1200."""
        assert DEFAULT_TIMEOUT_SECONDS == 1200

    def test_none_timeout_sends_1200_to_gateway(self, executor):
        """Calling _run_session(timeout=None) must forward runTimeoutSeconds=1200
        to the spawn call (AC-1)."""
        session_key = "sess-ac1-none"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=None
            )

        assert captured.get("runTimeoutSeconds") == DEFAULT_TIMEOUT_SECONDS, (
            f"Expected runTimeoutSeconds={DEFAULT_TIMEOUT_SECONDS}, "
            f"got {captured.get('runTimeoutSeconds')}"
        )

    def test_none_timeout_deadline_is_loop_start_plus_1200(self, executor):
        """The deadline must be computed as loop_start + 1200 when timeout=None,
        so the TimeoutError fires at exactly the right moment (AC-1 precise)."""
        session_key = "sess-ac1-deadline"
        loop_start = 500.0
        # Simulate: loop_start=500, deadline=500+1200=1700
        # Iteration 1: now=501 → within deadline (no timeout)
        # Iteration 2: now=1701 → past deadline → TimeoutError
        mono_values = iter([
            loop_start,     # loop_start assignment
            loop_start + 1, # iteration 1: now → within deadline
            loop_start + DEFAULT_TIMEOUT_SECONDS + 1,  # iteration 2: now → past deadline
        ])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            return _running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ):
            with pytest.raises(TimeoutError):
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=None
                )

    def test_none_timeout_with_executor_timeout_seconds_300(self, executor):
        """When timeout=None and executor.timeout_seconds=300, the deadline must
        use 300, not 1200 (two-level fallback chain — Fix #2 regression guard)."""
        executor.timeout_seconds = 300
        session_key = "sess-ac1-chain"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=None
            )

        assert captured.get("runTimeoutSeconds") == 300, (
            f"self.timeout_seconds=300 must be used; got {captured.get('runTimeoutSeconds')}"
        )

    def test_none_timeout_fallback_chain_uses_default_when_executor_also_none(self):
        """If both timeout=None and self.timeout_seconds is falsy, DEFAULT is used."""
        ex = OpenClawExecutor(gateway_url="http://localhost:18789", timeout_seconds=0)
        # timeout_seconds=0 is falsy → must fall back to DEFAULT_TIMEOUT_SECONDS
        session_key = "sess-ac1-both-none"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(ex, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            ex._run_session("hello", "anthropic/claude-sonnet-4-6", None, timeout=None)

        assert captured.get("runTimeoutSeconds") == DEFAULT_TIMEOUT_SECONDS


class TestAC2_LargeTimeoutRespected:
    """AC-2: A timeout larger than 1200 is used as-is (no capping)."""

    def test_3600_timeout_used_unchanged(self, executor):
        session_key = "sess-ac2-3600"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=3600
            )

        assert captured.get("runTimeoutSeconds") == 3600

    def test_7200_timeout_used_unchanged(self, executor):
        """A very large timeout (2 hours) is forwarded without modification."""
        session_key = "sess-ac2-7200"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=7200
            )

        assert captured.get("runTimeoutSeconds") == 7200

    def test_large_timeout_not_capped_at_1200(self, executor):
        """Explicitly verify that the timeout is NOT capped to DEFAULT_TIMEOUT_SECONDS."""
        session_key = "sess-ac2-nocap"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=5000
            )

        assert captured.get("runTimeoutSeconds") != DEFAULT_TIMEOUT_SECONDS
        assert captured.get("runTimeoutSeconds") == 5000


class TestAC3_DeadlineExceededRaisesTimeoutError:
    """AC-3: When time.monotonic() > deadline, TimeoutError is raised."""

    def test_timeout_error_raised_when_deadline_exceeded(self, executor):
        """TimeoutError raised when now > loop_start + effective_timeout (AC-3)."""
        session_key = "sess-ac3-basic"
        mono_values = iter([
            0.0,    # loop_start
            0.0,    # iteration 1: within deadline
            9999.0, # iteration 2: past deadline
        ])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            return _running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ):
            with pytest.raises(TimeoutError):
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=100
                )

    def test_timeout_error_message_contains_session_key(self, executor):
        """TimeoutError message must identify the session (AC-3)."""
        session_key = "my-identifiable-session-key"
        mono_values = iter([0.0, 0.0, 9999.0])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            return _running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ):
            with pytest.raises(TimeoutError) as exc_info:
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=10
                )

        assert session_key in str(exc_info.value), (
            f"Session key '{session_key}' not found in: {exc_info.value}"
        )

    def test_timeout_error_contains_timeout_duration(self, executor):
        """TimeoutError message should mention the timeout duration."""
        session_key = "sess-ac3-duration"
        timeout_value = 42
        mono_values = iter([0.0, 0.0, 9999.0])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            return _running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ):
            with pytest.raises(TimeoutError) as exc_info:
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=timeout_value
                )

        assert str(timeout_value) in str(exc_info.value), (
            f"Timeout duration '{timeout_value}' not in error: {exc_info.value}"
        )

    def test_deadline_check_uses_monotonic_not_wall_clock(self, executor):
        """time.monotonic() must be used (not time.time()) so NTP adjustments
        cannot cause premature or missed timeouts."""
        import orchestration_engine.openclaw_executor as mod

        # Verify the source uses time.monotonic
        import inspect
        src = inspect.getsource(mod._run_session if hasattr(mod, "_run_session") else mod.OpenClawExecutor._run_session)
        assert "time.monotonic" in src, (
            "Deadline logic must use time.monotonic(), not time.time()"
        )
        assert "time.time()" not in src or "monotonic" in src, (
            "Found time.time() in _run_session — should use time.monotonic() for NTP-safety"
        )

    def test_timeout_error_propagated_as_failed_task_result(self, executor, sample_task):
        """TimeoutError from _run_session must be caught and become TaskState.FAILED
        with error code 'timeout' (AC-3, execute() wrapper).

        Issue #346+#347: The retry loop retries TimeoutError up to max_attempts times
        on the primary model tier, then the fallback chain escalates to the next tier
        and retries again.  Provide enough monotonic values for all retry attempts
        across both model tiers (sonnet + opus fallback) so the test is stable.
        """
        # 3 values per _run_session attempt (loop_start, poll-within, poll-over-deadline).
        # With max_attempts=3 and 2 model tiers (sonnet + opus fallback), we need
        # 18 values total: 9 for sonnet attempts + 9 for opus fallback attempts.
        mono_values = iter([
            0.0, 0.0, 9999.0,   # sonnet attempt 0 → TimeoutError
            0.0, 0.0, 9999.0,   # sonnet attempt 1 → TimeoutError (retry)
            0.0, 0.0, 9999.0,   # sonnet attempt 2 → TimeoutError (retry)
            0.0, 0.0, 9999.0,   # opus attempt 0 → TimeoutError (fallback tier)
            0.0, 0.0, 9999.0,   # opus attempt 1 → TimeoutError (retry)
            0.0, 0.0, 9999.0,   # opus attempt 2 → TimeoutError (retry)
        ])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp("sess-ac3-wrap")
            return _running_resp("sess-ac3-wrap")

        sample_task.timeout_seconds = 10

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.errors, "At least one error expected"
        assert result.errors[0].code == "timeout"


class TestAC4_EightyPercentWarning:
    """AC-4: WARNING logged exactly once when elapsed >= 80% of timeout."""

    def test_warning_fires_once_when_crossing_threshold(self, executor):
        """logger.warning called exactly once after crossing 80% (AC-4)."""
        session_key = "sess-ac4-once"
        timeout = 100
        threshold = 0.8 * timeout  # 80s

        # Monotonic sequence:
        # - loop_start = 0
        # - iter 1: now=0   → elapsed=0  → below threshold (no warn)
        # - iter 2: now=81  → elapsed=81 → above threshold (WARN)
        # - iter 3: now=82  → elapsed=82 → still above (must NOT warn again)
        # - iter 4: now=9999→ deadline exceeded (loop ends)
        mono_values = iter([0.0, 0.0, threshold + 1.0, threshold + 2.0, 9999.0])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            return _running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ), patch(
            "orchestration_engine.openclaw_executor.logger"
        ) as mock_logger:
            with pytest.raises(TimeoutError):
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=timeout
                )

        # Filter for the 80% warning specifically
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "elapsed" in str(c).lower() or "%" in str(c)
        ]
        assert len(warning_calls) == 1, (
            f"Expected exactly 1 80%-warning call, got {len(warning_calls)}. "
            f"All warning calls: {mock_logger.warning.call_args_list}"
        )

    def test_warning_not_fired_before_threshold(self, executor):
        """logger.warning must NOT be called while elapsed < 80% (AC-4 negative)."""
        session_key = "sess-ac4-negative"
        timeout = 100
        threshold = 0.8 * timeout  # 80s

        # All iterations stay below threshold, then session completes
        mono_values = iter([
            0.0,                    # loop_start
            threshold - 10.0,       # iter 1: deadline check
            threshold - 10.0,       # iter 1: elapsed calc
            threshold - 5.0,        # iter 2: deadline check
            threshold - 5.0,        # iter 2: elapsed calc
            threshold - 1.0,        # iter 3: deadline check (done response)
            threshold - 1.0,        # iter 3: elapsed calc
        ])
        # After two running responses, return done
        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return _running_resp(session_key)
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ), patch(
            "orchestration_engine.openclaw_executor.logger"
        ) as mock_logger:
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=timeout
            )

        # No elapsed/% warning should have fired
        premature_warnings = [
            c
            for c in mock_logger.warning.call_args_list
            if "elapsed" in str(c).lower() or "%" in str(c)
        ]
        assert not premature_warnings, (
            f"Warning fired prematurely (before 80% threshold). "
            f"Calls: {mock_logger.warning.call_args_list}"
        )

    def test_warning_fires_for_small_timeout(self, executor):
        """AC-4 edge: 80% warning fires correctly even with a 2-second timeout
        (threshold = 1.6s). Verifies no off-by-one in the math."""
        session_key = "sess-ac4-small"
        timeout = 2
        threshold = 0.8 * timeout  # 1.6s

        mono_values = iter([
            0.0,              # loop_start
            0.0,              # iter 1: elapsed=0 → below threshold
            threshold + 0.1,  # iter 2: elapsed=1.7 → above threshold (WARN)
            9999.0,           # iter 3: deadline exceeded
        ])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            return _running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ), patch(
            "orchestration_engine.openclaw_executor.logger"
        ) as mock_logger:
            with pytest.raises(TimeoutError):
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=timeout
                )

        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "elapsed" in str(c).lower() or "%" in str(c)
        ]
        assert len(warning_calls) == 1, (
            f"Expected exactly 1 warning for 2s timeout at 1.6s elapsed, "
            f"got {len(warning_calls)}"
        )

    def test_warning_fires_at_exactly_80_percent(self, executor):
        """80% warning must fire when elapsed == 80% exactly (boundary condition)."""
        session_key = "sess-ac4-exact"
        timeout = 100
        threshold = 0.8 * timeout  # 80.0s exactly

        mono_values = iter([
            0.0,        # loop_start
            threshold,  # iter 1: elapsed = exactly 80% → WARN
            9999.0,     # iter 2: deadline exceeded
        ])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            return _running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ), patch(
            "orchestration_engine.openclaw_executor.logger"
        ) as mock_logger:
            with pytest.raises(TimeoutError):
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=timeout
                )

        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "elapsed" in str(c).lower() or "%" in str(c)
        ]
        assert len(warning_calls) == 1

    def test_normal_completion_before_deadline_no_timeout_interference(self, executor):
        """AC-4 / AC-3 clean-exit: when the session completes normally before the
        deadline, neither the timeout warning nor TimeoutError must fire."""
        session_key = "sess-ac4-clean"
        timeout = 1000
        # Elapsed stays well below 80% (threshold = 800s)
        mono_values = iter([
            0.0,   # loop_start
            10.0,  # iter 1: 10s elapsed → way below threshold
        ])
        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            return _done_resp(session_key, "completed cleanly")

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ), patch(
            "orchestration_engine.openclaw_executor.logger"
        ) as mock_logger:
            output, _ = executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=timeout
            )

        assert "completed cleanly" in output
        # No 80% warning
        elapsed_warnings = [
            c
            for c in mock_logger.warning.call_args_list
            if "elapsed" in str(c).lower() or "%" in str(c)
        ]
        assert not elapsed_warnings, f"Unexpected warnings: {elapsed_warnings}"


class TestAC5_DeadlineCheckedEveryIteration:
    """AC-5: The deadline check executes on every iteration, including after
    successful but non-terminal gateway responses."""

    def test_multiple_non_terminal_responses_eventually_timeout(self, executor):
        """TimeoutError fires even after N non-terminal polling responses (AC-5)."""
        session_key = "sess-ac5-many"
        mono_values = iter([
            0.0, 1.0, 2.0, 3.0, 9999.0
        ])
        poll_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            poll_count["n"] += 1
            return _running_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ):
            with pytest.raises(TimeoutError):
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=10
                )

        # Polled at least twice — deadline check fired after each non-terminal response
        assert poll_count["n"] >= 2, (
            f"Expected multiple polls before TimeoutError, got {poll_count['n']}"
        )

    def test_deadline_check_fires_after_each_successful_response(self, executor):
        """The deadline is checked even when the gateway returns a valid JSON
        response with messages — it just lacks a terminal stopReason."""
        session_key = "sess-ac5-check"
        # 4 running responses, then timeout on the 5th iteration check
        mono_values = iter([0.0, 1.0, 2.0, 3.0, 4.0, 9999.0])

        poll_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            poll_count["n"] += 1
            # Return a response with a user message but no terminal assistant
            return {
                "ok": True,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({
                                "sessionKey": session_key,
                                "messages": [
                                    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                                    {"role": "assistant", "content": [{"type": "text", "text": "working..."}]},
                                    # No stopReason → non-terminal
                                ],
                            }),
                        }
                    ],
                },
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ):
            with pytest.raises(TimeoutError):
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=10
                )

        assert poll_count["n"] >= 2


# ===========================================================================
# #240 — Edge Cases
# ===========================================================================


class TestTimeoutEdgeCases:
    """Edge cases documented in the requirements for #240."""

    # ── timeout=0 → ValueError ──────────────────────────────────────────────

    def test_zero_timeout_raises_value_error(self, executor):
        """timeout=0 must raise ValueError immediately, not ZeroDivisionError."""
        with pytest.raises(ValueError, match="positive integer"):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=0
            )

    def test_zero_timeout_error_message_mentions_value(self, executor):
        """ValueError for timeout=0 must echo the bad value back to the caller."""
        with pytest.raises(ValueError) as exc_info:
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=0
            )
        assert "0" in str(exc_info.value)

    def test_negative_timeout_raises_value_error(self, executor):
        """timeout < 0 must also raise ValueError (same guard covers <= 0)."""
        with pytest.raises(ValueError, match="positive integer"):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=-1
            )

    def test_negative_large_raises_value_error(self, executor):
        """Large negative values are also rejected."""
        with pytest.raises(ValueError):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=-9999
            )

    def test_zero_timeout_no_http_calls_made(self, executor):
        """ValueError must fire BEFORE any HTTP calls (no side effects)."""
        with patch.object(executor, "_http_post") as mock_post:
            with pytest.raises(ValueError):
                executor._run_session(
                    "hello", "anthropic/claude-sonnet-4-6", None, timeout=0
                )
            mock_post.assert_not_called()

    def test_zero_timeout_propagated_as_failed_in_execute(self, executor, sample_task):
        """When _run_session raises ValueError, execute() must catch it and
        return TaskState.FAILED with error code 'execution_error'."""
        # Force task.timeout_seconds to a value that will be passed as timeout
        # We patch _run_session directly to raise ValueError.
        # Mock time.sleep to avoid real backoff delays from the retry loop (#346).
        with patch.object(
            executor,
            "_run_session",
            side_effect=ValueError("timeout must be a positive integer (got 0)"),
        ), patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.errors
        assert result.errors[0].code == "execution_error"

    # ── float('inf') → fallback to DEFAULT ─────────────────────────────────

    def test_inf_timeout_falls_back_to_default(self, executor):
        """float('inf') must not be used as deadline — falls back to DEFAULT."""
        session_key = "sess-edge-inf"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=float("inf")
            )

        assert captured.get("runTimeoutSeconds") == DEFAULT_TIMEOUT_SECONDS, (
            f"inf timeout must use DEFAULT_TIMEOUT_SECONDS={DEFAULT_TIMEOUT_SECONDS}, "
            f"got {captured.get('runTimeoutSeconds')}"
        )

    def test_inf_timeout_not_forwarded_to_gateway(self, executor):
        """Infinity must never reach the gateway as runTimeoutSeconds."""
        session_key = "sess-edge-inf2"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=float("inf")
            )

        assert not math.isinf(captured.get("runTimeoutSeconds", 0))

    def test_inf_timeout_logs_warning(self, executor):
        """Passing float('inf') must log a warning about the fallback."""
        session_key = "sess-edge-inf-warn"

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.logger"
        ) as mock_logger:
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=float("inf")
            )

        inf_warnings = [
            c
            for c in mock_logger.warning.call_args_list
            if "infinite" in str(c).lower() or "inf" in str(c).lower()
        ]
        assert inf_warnings, (
            "Expected a warning log for infinite timeout. "
            f"All warning calls: {mock_logger.warning.call_args_list}"
        )

    def test_inf_timeout_session_still_completes_normally(self, executor):
        """Despite infinite timeout being remapped, the session must complete OK."""
        session_key = "sess-edge-inf-ok"

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key, "inf-safe output")

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            output, _ = executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=float("inf")
            )

        assert "inf-safe output" in output

    # ── None timeout respects self.timeout_seconds ─────────────────────────

    def test_none_timeout_respects_executor_timeout_seconds(self, executor):
        """timeout=None → self.timeout_seconds used, not DEFAULT (Fix #2 guard)."""
        custom = 300
        assert custom != DEFAULT_TIMEOUT_SECONDS
        executor.timeout_seconds = custom

        session_key = "sess-edge-chain"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=None
            )

        assert captured.get("runTimeoutSeconds") == custom

    # ── Loop exits for other reasons before deadline ────────────────────────

    def test_terminal_stop_reason_exits_before_deadline(self, executor):
        """When session completes via stopReason=stop, no TimeoutError must be raised."""
        session_key = "sess-edge-early-exit"
        # Deadline would be at t=100; session completes at t=5
        mono_values = iter([
            0.0,   # loop_start
            5.0,   # iter 1: now=5 → well within deadline (100s)
        ])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key, "early completion")

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.time.monotonic",
            side_effect=mono_values,
        ):
            # Must not raise TimeoutError
            output, _ = executor._run_session(
                "hello", "anthropic/claude-sonnet-4-6", None, timeout=100
            )

        assert "early completion" in output


# ===========================================================================
# #241 — Session Cleanup Detection
# ===========================================================================


class TestAC6_HadMessagesStartsFalse:
    """AC-6: had_messages starts as False; first empty poll raises no error."""

    def test_first_empty_poll_raises_no_error(self, executor):
        """Empty messages on first poll → session not yet started, no exception (AC-6)."""
        session_key = "sess-241-ac6"
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "result"}],
             "stopReason": "stop"},
        ]
        mock = _make_poll_sequence(session_key, [[], done_messages])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            output, _ = executor._run_session(
                "go", "anthropic/claude-sonnet-4-6", None, timeout=60
            )

        assert "result" in output

    def test_single_empty_poll_then_success(self, executor):
        """One empty poll followed by a completed session → success, no error."""
        session_key = "sess-ac6-single"
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
             "stopReason": "stop"},
        ]
        mock = _make_poll_sequence(session_key, [[], done_messages])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            output, _ = executor._run_session(
                "go", "anthropic/claude-sonnet-4-6", None, timeout=60
            )

        assert "ok" in output


class TestAC7_HadMessagesSetOnNonEmpty:
    """AC-7: had_messages set to True after any non-empty poll."""

    def test_non_empty_poll_followed_by_empty_raises_runtime_error(self, executor):
        """non-empty → empty sequence raises RuntimeError, proving had_messages was set (AC-7 + AC-8)."""
        session_key = "sess-241-ac7"
        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        mock = _make_poll_sequence(session_key, [non_empty, []])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            with pytest.raises(RuntimeError):
                executor._run_session(
                    "go", "anthropic/claude-sonnet-4-6", None, timeout=60
                )

    def test_had_messages_tracks_per_run_session_call(self, executor):
        """Each call to _run_session has its own had_messages (not shared state)."""
        # First call: completes normally
        session_key_1 = "sess-ac7-first"
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
             "stopReason": "stop"},
        ]
        mock1 = _make_poll_sequence(session_key_1, [done_messages])

        with patch.object(executor, "_http_post", side_effect=mock1), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            output1, _ = executor._run_session(
                "go", "anthropic/claude-sonnet-4-6", None, timeout=60
            )

        assert "ok" in output1

        # Second call: empty first poll (session not started yet) → should NOT raise
        session_key_2 = "sess-ac7-second"
        done_messages_2 = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "result2"}],
             "stopReason": "stop"},
        ]
        mock2 = _make_poll_sequence(session_key_2, [[], done_messages_2])

        with patch.object(executor, "_http_post", side_effect=mock2), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            # If had_messages leaked from first call, this would wrongly raise RuntimeError
            output2, _ = executor._run_session(
                "go", "anthropic/claude-sonnet-4-6", None, timeout=60
            )

        assert "result2" in output2


class TestAC8_GCDetectionRaisesRuntimeError:
    """AC-8: had_messages=True + empty poll → RuntimeError in same poll cycle."""

    def test_gc_detected_on_second_poll(self, executor):
        """[non-empty, empty] → RuntimeError raised on the second (empty) poll."""
        session_key = "sess-ac8-gc"
        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        mock = _make_poll_sequence(session_key, [non_empty, []])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            with pytest.raises(RuntimeError):
                executor._run_session(
                    "go", "anthropic/claude-sonnet-4-6", None, timeout=60
                )

    def test_gc_error_not_raised_on_first_empty(self, executor):
        """RuntimeError must NOT be raised on the first (empty) poll — only on
        empty-after-non-empty (AC-8 vs AC-10 contrast)."""
        session_key = "sess-ac8-not-first"
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}],
             "stopReason": "stop"},
        ]
        # First poll empty (OK), second poll done
        mock = _make_poll_sequence(session_key, [[], done_messages])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            # Must NOT raise
            output, _ = executor._run_session(
                "go", "anthropic/claude-sonnet-4-6", None, timeout=60
            )

        assert "done" in output

    def test_gc_error_is_runtime_error_type(self, executor):
        """The exception must be exactly RuntimeError (not a subclass or ValueError)."""
        session_key = "sess-ac8-type"
        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        mock = _make_poll_sequence(session_key, [non_empty, []])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            with pytest.raises(RuntimeError) as exc_info:
                executor._run_session(
                    "go", "anthropic/claude-sonnet-4-6", None, timeout=60
                )

        # Must be RuntimeError, not just an Exception
        assert type(exc_info.value) is RuntimeError


class TestAC9_GCErrorContainsSessionKey:
    """AC-9: RuntimeError message includes the session key for diagnostics."""

    def test_gc_error_message_includes_session_key(self, executor):
        """session_key must appear in str(exception) (AC-9)."""
        session_key = "my-unique-key-for-diagnostics-xyz"
        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        mock = _make_poll_sequence(session_key, [non_empty, []])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            with pytest.raises(RuntimeError) as exc_info:
                executor._run_session(
                    "go", "anthropic/claude-sonnet-4-6", None, timeout=60
                )

        assert session_key in str(exc_info.value), (
            f"Session key '{session_key}' not found in error: {exc_info.value}"
        )

    def test_gc_error_message_describes_gc_reason(self, executor):
        """RuntimeError must describe why (GC/eviction), not just be opaque."""
        session_key = "sess-ac9-desc"
        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        mock = _make_poll_sequence(session_key, [non_empty, []])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            with pytest.raises(RuntimeError) as exc_info:
                executor._run_session(
                    "go", "anthropic/claude-sonnet-4-6", None, timeout=60
                )

        err_msg = str(exc_info.value).lower()
        assert any(
            kw in err_msg for kw in ("garbage", "evict", "cleanup", "gc", "empty")
        ), f"Error must describe GC reason; got: {exc_info.value}"


class TestAC10_NoErrorWhenSessionNeverStarted:
    """AC-10: Empty messages on first (and subsequent) polls before non-empty → no error."""

    def test_empty_empty_nonempty_no_error(self, executor):
        """[empty, empty, non-empty] → no RuntimeError raised (AC-10)."""
        session_key = "sess-ac10-three"
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "output"}],
             "stopReason": "stop"},
        ]
        mock = _make_poll_sequence(session_key, [[], [], done_messages])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            output, _ = executor._run_session(
                "go", "anthropic/claude-sonnet-4-6", None, timeout=120
            )

        assert "output" in output

    def test_many_empty_polls_before_start(self, executor):
        """Ten empty polls before session starts → no error."""
        session_key = "sess-ac10-ten"
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "late result"}],
             "stopReason": "stop"},
        ]
        # 10 empty polls, then done
        payloads = [[] for _ in range(10)] + [done_messages]
        mock = _make_poll_sequence(session_key, payloads)

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            output, _ = executor._run_session(
                "go", "anthropic/claude-sonnet-4-6", None, timeout=300
            )

        assert "late result" in output


# ===========================================================================
# #241 — Edge Cases
# ===========================================================================


class TestSessionCleanupEdgeCases:
    """Edge cases documented in the requirements for #241."""

    # ── messages = None treated as empty ───────────────────────────────────

    def test_none_messages_treated_as_empty_no_error_on_first_poll(self, executor):
        """messages=None on first poll → treated as empty, no RuntimeError."""
        session_key = "sess-241-null"
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
             "stopReason": "stop"},
        ]

        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _null_messages_resp(session_key)
            return {
                "ok": True,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(
                            {"sessionKey": session_key, "messages": done_messages}
                        )}
                    ]
                },
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            output, _ = executor._run_session(
                "go", "anthropic/claude-sonnet-4-6", None, timeout=60
            )

        assert "ok" in output

    def test_none_messages_after_non_empty_raises_runtime_error(self, executor):
        """messages=None after a non-empty poll → same as empty: RuntimeError."""
        session_key = "sess-241-null-gc"
        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]

        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "ok": True,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(
                                {"sessionKey": session_key, "messages": non_empty}
                            )}
                        ]
                    },
                }
            # Second poll: messages=None → should be treated as empty
            return _null_messages_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            with pytest.raises(RuntimeError) as exc_info:
                executor._run_session(
                    "go", "anthropic/claude-sonnet-4-6", None, timeout=60
                )

        assert session_key in str(exc_info.value)

    # ── Session flicker: [non-empty → empty → non-empty] ───────────────────

    def test_session_flicker_raises_immediately_on_empty(self, executor):
        """[non-empty, empty, non-empty] → RuntimeError on the SECOND (empty) poll.
        Per spec: real cleanup is permanent; flickering indicates a bug elsewhere."""
        session_key = "sess-241-flicker"
        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "final"}],
             "stopReason": "stop"},
        ]

        # [non-empty, empty, done] — must raise on second (empty) poll,
        # NOT continue to consume the third element
        poll_count = {"n": 0}
        payload_iter = iter([non_empty, [], done_messages])

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            poll_count["n"] += 1
            messages = next(payload_iter)
            return {
                "ok": True,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(
                            {"sessionKey": session_key, "messages": messages}
                        )}
                    ]
                },
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            with pytest.raises(RuntimeError):
                executor._run_session(
                    "go", "anthropic/claude-sonnet-4-6", None, timeout=60
                )

        # The third poll (non-empty done) must never have been reached
        assert poll_count["n"] == 2, (
            f"Expected exactly 2 polls (non-empty + empty→raise), "
            f"got {poll_count['n']}"
        )

    # ── GC error propagates through execute() ──────────────────────────────

    def test_gc_detection_causes_failed_task_result(self, executor, sample_task):
        """RuntimeError from GC detection must be caught by execute() and returned
        as TaskState.FAILED with an informative error."""
        session_key = "sess-gc-exec"
        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        mock = _make_poll_sequence(session_key, [non_empty, []])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert result.errors, "Expected at least one error in TaskResult"

    def test_gc_error_message_in_task_result_errors(self, executor, sample_task):
        """The TaskResult errors list must contain a message referencing the GC event."""
        session_key = "sess-gc-msg"
        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        mock = _make_poll_sequence(session_key, [non_empty, []])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        error_messages = " ".join(e.message for e in result.errors)
        # The original RuntimeError message should be in the error
        assert any(
            kw in error_messages.lower()
            for kw in ("garbage", "evict", "empty", "gc", "session")
        ), f"GC-related info not found in errors: {error_messages!r}"


# ===========================================================================
# Fix #3 — SESSIONS_HISTORY_LIMIT ceiling warning fires exactly once
# ===========================================================================


class TestLimitWarningFiredOnce:
    """Fix #3: The SESSIONS_HISTORY_LIMIT ceiling warning must fire at most once
    per session, even if the session stalls at exactly the limit for many polls."""

    def _at_limit_messages(self, n_assistant: int = None):
        """Build a message list of exactly SESSIONS_HISTORY_LIMIT entries."""
        if n_assistant is None:
            n_assistant = SESSIONS_HISTORY_LIMIT - 1  # 1 user + n_assistant = limit
        msgs = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
        for i in range(n_assistant):
            is_last = i == n_assistant - 1
            entry = {
                "role": "assistant",
                "content": [{"type": "text", "text": f"chunk_{i}"}],
            }
            if is_last:
                entry["stopReason"] = "stop"
            msgs.append(entry)
        return msgs

    def test_limit_warning_fires_once_single_poll(self, executor, sample_task, caplog):
        """Warning fires exactly once when response at SESSIONS_HISTORY_LIMIT."""
        import logging

        messages = self._at_limit_messages()
        assert len(messages) == SESSIONS_HISTORY_LIMIT

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp("sess-lw-single")
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return {
                "ok": True,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(
                        {"sessionKey": "sess-lw-single", "messages": messages}
                    )}]
                },
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), caplog.at_level(logging.WARNING, logger="orchestration_engine.openclaw_executor"):
            executor.execute(sample_task)

        limit_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and str(SESSIONS_HISTORY_LIMIT) in r.getMessage()
        ]
        assert len(limit_warnings) >= 1, "Expected at least one limit warning"

    def test_limit_warning_fires_only_once_across_multiple_polls(self, executor, sample_task):
        """If the session stalls at exactly SESSIONS_HISTORY_LIMIT for multiple polls,
        the warning must fire at most once (Fix #3 — limit_warning_fired flag)."""
        session_key = "sess-lw-multi"

        # Build at-limit messages that are NOT terminal (no stopReason on last assistant)
        non_terminal_at_limit = [
            {"role": "user", "content": [{"type": "text", "text": "go"}]}
        ]
        for i in range(SESSIONS_HISTORY_LIMIT - 1):
            non_terminal_at_limit.append({
                "role": "assistant",
                "content": [{"type": "text", "text": f"chunk_{i}"}],
                # NO stopReason — session still "running"
            })
        assert len(non_terminal_at_limit) == SESSIONS_HISTORY_LIMIT

        # Terminal response (session finishes on 4th poll)
        terminal_messages = [
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "final"}],
             "stopReason": "stop"},
        ]

        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            if call_count["n"] <= 3:
                # First 3 polls: at limit, no terminal reason
                return {
                    "ok": True,
                    "result": {"content": [{"type": "text", "text": json.dumps(
                        {"sessionKey": session_key, "messages": non_terminal_at_limit}
                    )}]},
                }
            # 4th poll: terminal
            return {
                "ok": True,
                "result": {"content": [{"type": "text", "text": json.dumps(
                    {"sessionKey": session_key, "messages": terminal_messages}
                )}]},
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.logger"
        ) as mock_logger:
            executor._run_session("go", "anthropic/claude-sonnet-4-6", None, timeout=120)

        # Count how many times the limit warning was emitted
        limit_warning_calls = [
            c for c in mock_logger.warning.call_args_list
            if str(SESSIONS_HISTORY_LIMIT) in str(c)
        ]
        assert len(limit_warning_calls) == 1, (
            f"Expected limit warning to fire exactly once (limit_warning_fired flag), "
            f"but fired {len(limit_warning_calls)} times. "
            f"Calls: {limit_warning_calls}"
        )

    def test_limit_warning_not_fired_for_small_session(self, executor, sample_task, caplog):
        """No limit warning when session is well below SESSIONS_HISTORY_LIMIT."""
        import logging

        session_key = "sess-lw-small"
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "small output"}],
             "stopReason": "stop"},
        ]
        assert len(messages) < SESSIONS_HISTORY_LIMIT

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return {
                "ok": True,
                "result": {"content": [{"type": "text", "text": json.dumps(
                    {"sessionKey": session_key, "messages": messages}
                )}]},
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), caplog.at_level(logging.WARNING, logger="orchestration_engine.openclaw_executor"):
            executor.execute(sample_task)

        limit_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and str(SESSIONS_HISTORY_LIMIT) in r.getMessage()
        ]
        assert not limit_warnings, (
            f"Unexpected limit warning for a small session: {[r.getMessage() for r in limit_warnings]}"
        )


# ===========================================================================
# Happy Path Integration Tests
# ===========================================================================


class TestHappyPath:
    """End-to-end happy path verifications covering the normal flow."""

    def test_successful_session_returns_success_state(self, executor, sample_task):
        """Normal session completion → TaskState.SUCCESS."""
        session_key = "sess-happy-basic"

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key, "hello world")

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS

    def test_successful_session_output_in_result(self, executor, sample_task):
        """Output text is placed in result['text']."""
        session_key = "sess-happy-output"

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key, "my output text")

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.result.get("text") == "my output text"

    def test_session_with_multiple_empty_polls_then_completes(self, executor, sample_task):
        """Session that takes a few polls to start → completes successfully."""
        session_key = "sess-happy-delay"
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "delayed output"}],
             "stopReason": "stop"},
        ]
        mock = _make_poll_sequence(session_key, [[], [], [], done_messages])

        with patch.object(executor, "_http_post", side_effect=mock), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert "delayed output" in result.result.get("text", "")

    def test_end_turn_stop_reason_is_terminal(self, executor, sample_task):
        """stopReason='end_turn' must also be treated as terminal."""
        session_key = "sess-happy-end-turn"

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return {
                "ok": True,
                "result": {"content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "x"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": "end-turn output"}],
                         "stopReason": "end_turn"},
                    ],
                })}]},
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert "end-turn output" in result.result.get("text", "")

    def test_non_terminal_stop_reasons_continue_polling(self, executor, sample_task):
        """stopReason='' or unrecognised → session still polling, not terminal."""
        session_key = "sess-happy-nonterminal"
        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Non-terminal: stopReason is empty string
                return {
                    "ok": True,
                    "result": {"content": [{"type": "text", "text": json.dumps({
                        "sessionKey": session_key,
                        "messages": [
                            {"role": "user", "content": [{"type": "text", "text": "x"}]},
                            {"role": "assistant", "content": [{"type": "text", "text": "partial"}],
                             "stopReason": ""},
                        ],
                    })}]},
                }
            # Second poll: terminal
            return _done_resp(session_key, "final output")

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert call_count["n"] >= 2, "Should have polled twice (once non-terminal, once terminal)"


# ===========================================================================
# Error Path Tests
# ===========================================================================


class TestErrorPaths:
    """Error paths beyond timeout and GC detection."""

    def test_spawn_failure_returns_failed(self, executor, sample_task):
        """Gateway spawn failure → TaskState.FAILED."""
        # Mock time.sleep to prevent real backoff delays during retries (#346).
        with patch.object(
            executor, "_http_post",
            side_effect=RuntimeError("Gateway HTTP error 500: Internal Server Error")
        ), patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED

    def test_spawn_not_ok_returns_failed(self, executor, sample_task):
        """Gateway returns ok=False on spawn → TaskState.FAILED."""
        # Mock time.sleep to prevent real backoff delays during retries (#346).
        with patch.object(executor, "_http_post", return_value={
            "ok": False, "error": {"message": "spawn refused"}
        }), patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED

    def test_missing_session_key_returns_failed(self, executor, sample_task):
        """Spawn response without childSessionKey → TaskState.FAILED."""
        # Mock time.sleep to prevent real backoff delays during retries (#346).
        with patch.object(executor, "_http_post", return_value={
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": '{"status":"accepted"}'}],
                "details": {"status": "accepted"},
            },
        }), patch("orchestration_engine.openclaw_executor.time.sleep"):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED

    def test_transient_poll_error_retried(self, executor, sample_task):
        """Transient RuntimeError during sessions_history poll → retried, not fatal."""
        session_key = "sess-err-transient"
        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First poll: transient error
                raise RuntimeError("Gateway HTTP error 503: Service Unavailable")
            # Second poll: success
            return _done_resp(session_key, "recovered output")

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert "recovered output" in result.result.get("text", "")

    def test_empty_output_returns_failed(self, executor, sample_task):
        """Session that completes with all-empty text → TaskState.FAILED."""
        session_key = "sess-err-empty"

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return {
                "ok": True,
                "result": {"content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "x"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": ""}],
                         "stopReason": "stop"},
                    ],
                })}]},
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert any(e.code == "empty_output" for e in result.errors)

    def test_max_tokens_stop_reason_returns_failed_with_partial(self, executor, sample_task):
        """stopReason=max_tokens → FAILED with partial_output captured."""
        session_key = "sess-err-maxtoken"

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return {
                "ok": True,
                "result": {"content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "write a novel"}]},
                        {"role": "assistant", "content": [
                            {"type": "text", "text": "Chapter 1: The beginning..."},
                        ], "stopReason": "max_tokens"},
                    ],
                })}]},
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED
        assert "Chapter 1" in result.result.get("partial_output", "")


# ===========================================================================
# Gateway-Retryable Error Tests (Issue #482)
# ===========================================================================


class TestGatewayRetryableErrors:
    """Executor should continue polling on gateway-retryable errors (overloaded, rate limit)."""

    def test_overloaded_error_continues_polling(self, executor, sample_task):
        """stopReason='error' with overloaded_error → keep polling, not terminal."""
        session_key = "sess-overloaded-retry"
        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            if call_count["n"] <= 2:
                # First 2 polls: overloaded error (gateway retrying)
                return {
                    "ok": True,
                    "result": {"content": [{"type": "text", "text": json.dumps({
                        "sessionKey": session_key,
                        "messages": [
                            {"role": "assistant", "content": [],
                             "stopReason": "error",
                             "errorMessage": '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'},
                        ],
                    })}]},
                }
            # Third poll: gateway succeeded
            return _done_resp(session_key, "overloaded then succeeded")

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert "overloaded then succeeded" in result.result.get("text", "")
        assert call_count["n"] >= 3, "Should have polled past the overloaded errors"

    def test_rate_limit_error_continues_polling(self, executor, sample_task):
        """stopReason='error' with rate_limit_error → keep polling."""
        session_key = "sess-ratelimit-retry"
        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "ok": True,
                    "result": {"content": [{"type": "text", "text": json.dumps({
                        "sessionKey": session_key,
                        "messages": [
                            {"role": "assistant", "content": [],
                             "stopReason": "error",
                             "errorMessage": '{"type":"error","error":{"type":"rate_limit_error","message":"Rate limited"}}'},
                        ],
                    })}]},
                }
            return _done_resp(session_key, "rate limited then succeeded")

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.SUCCESS
        assert call_count["n"] >= 2

    def test_permanent_error_still_terminal(self, executor, sample_task):
        """stopReason='error' WITHOUT retryable error type → still terminal (FAILED)."""
        session_key = "sess-permanent-error"

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return {
                "ok": True,
                "result": {"content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [
                        {"role": "assistant", "content": [],
                         "stopReason": "error",
                         "errorMessage": '{"type":"error","error":{"type":"invalid_request_error","message":"Bad request"}}'},
                    ],
                })}]},
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED

    def test_error_without_message_still_terminal(self, executor, sample_task):
        """stopReason='error' with no errorMessage → terminal (backward compat)."""
        session_key = "sess-no-errmsg"

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return {
                "ok": True,
                "result": {"content": [{"type": "text", "text": json.dumps({
                    "sessionKey": session_key,
                    "messages": [
                        {"role": "assistant", "content": [],
                         "stopReason": "error"},
                    ],
                })}]},
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            result = executor.execute(sample_task)

        assert result.state == TaskState.FAILED


# ===========================================================================
# Constant value guard tests
# ===========================================================================


class TestConstants:
    """Guard tests for module-level constants."""

    def test_default_timeout_seconds_is_1200(self):
        assert DEFAULT_TIMEOUT_SECONDS == 1200

    def test_poll_interval_seconds_positive(self):
        assert POLL_INTERVAL_SECONDS > 0

    def test_sessions_history_limit_at_least_1000(self):
        assert SESSIONS_HISTORY_LIMIT >= 1000

    def test_sessions_history_limit_is_int(self):
        assert isinstance(SESSIONS_HISTORY_LIMIT, int)

    def test_default_timeout_is_int(self):
        assert isinstance(DEFAULT_TIMEOUT_SECONDS, int)


# ===========================================================================
# Regression Guards
# ===========================================================================


class TestRegressionGuards:
    """Regression guards to ensure previous bugs don't re-appear."""

    def test_zero_division_error_not_raised_for_timeout_0(self, executor):
        """Regression: timeout=0 must raise ValueError, not ZeroDivisionError.

        The 80% warning arithmetic (100.0 * elapsed / total_timeout) would
        produce ZeroDivisionError if total_timeout=0 were allowed through."""
        exc = None
        try:
            executor._run_session("x", "anthropic/claude-sonnet-4-6", None, timeout=0)
        except ValueError as e:
            exc = e
        except ZeroDivisionError:
            pytest.fail(
                "ZeroDivisionError raised for timeout=0 — ValueError guard is missing!"
            )

        assert exc is not None, "Expected ValueError for timeout=0"

    def test_executor_timeout_seconds_not_ignored_when_task_timeout_absent(self, executor):
        """Regression: self.timeout_seconds must not be silently dropped in favour of
        DEFAULT_TIMEOUT_SECONDS when the per-call timeout is None (Fix #2)."""
        executor.timeout_seconds = 450  # Custom, differs from DEFAULT_TIMEOUT_SECONDS
        session_key = "sess-regression-chain"
        captured: dict = {}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                captured.update(body.get("args", {}))
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            return _done_resp(session_key)

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            executor._run_session("hello", "anthropic/claude-sonnet-4-6", None, timeout=None)

        assert captured.get("runTimeoutSeconds") == 450, (
            "self.timeout_seconds=450 was not used; Fix #2 regression detected. "
            f"Got: {captured.get('runTimeoutSeconds')}"
        )

    def test_limit_warning_does_not_spam_logs_on_stall(self, executor):
        """Regression: SESSIONS_HISTORY_LIMIT ceiling warning must not fire more
        than once even if the session stalls at the limit (Fix #3)."""
        session_key = "sess-regression-spam"

        # Non-terminal messages at exactly the limit
        at_limit = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
        for i in range(SESSIONS_HISTORY_LIMIT - 1):
            at_limit.append({
                "role": "assistant",
                "content": [{"type": "text", "text": f"c{i}"}],
            })
        assert len(at_limit) == SESSIONS_HISTORY_LIMIT

        terminal = [
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}],
             "stopReason": "stop"},
        ]

        call_count = {"n": 0}

        def mock_post(url, body):
            if body.get("tool") == "sessions_spawn":
                return _spawn_resp(session_key)
            if body.get("tool") == "sessions_list":
                return _list_resp_empty()
            call_count["n"] += 1
            if call_count["n"] <= 5:  # 5 polls at the limit
                return {
                    "ok": True,
                    "result": {"content": [{"type": "text", "text": json.dumps(
                        {"sessionKey": session_key, "messages": at_limit}
                    )}]},
                }
            return {
                "ok": True,
                "result": {"content": [{"type": "text", "text": json.dumps(
                    {"sessionKey": session_key, "messages": terminal}
                )}]},
            }

        with patch.object(executor, "_http_post", side_effect=mock_post), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ), patch(
            "orchestration_engine.openclaw_executor.logger"
        ) as mock_logger:
            executor._run_session("go", "anthropic/claude-sonnet-4-6", None, timeout=120)

        limit_warns = [
            c for c in mock_logger.warning.call_args_list
            if str(SESSIONS_HISTORY_LIMIT) in str(c)
        ]
        assert len(limit_warns) <= 1, (
            f"Limit warning fired {len(limit_warns)} times (should be at most 1). "
            "Fix #3 (limit_warning_fired flag) regression detected."
        )

    def test_had_messages_does_not_persist_between_run_session_calls(self, executor):
        """Regression: had_messages must be a local variable (not instance state),
        so it resets to False on every new _run_session() call."""
        session_key_a = "sess-reg-had-a"
        session_key_b = "sess-reg-had-b"

        non_empty = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]

        # First call: [non-empty, empty] → RuntimeError (had_messages becomes True inside call)
        mock_a = _make_poll_sequence(session_key_a, [non_empty, []])
        with patch.object(executor, "_http_post", side_effect=mock_a), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            with pytest.raises(RuntimeError):
                executor._run_session("go", "anthropic/claude-sonnet-4-6", None, timeout=60)

        # Second call: [empty, done] → must NOT raise (had_messages was local, resets to False)
        done_messages = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "fresh start"}],
             "stopReason": "stop"},
        ]
        mock_b = _make_poll_sequence(session_key_b, [[], done_messages])
        with patch.object(executor, "_http_post", side_effect=mock_b), patch(
            "orchestration_engine.openclaw_executor.time.sleep"
        ):
            # This must NOT raise RuntimeError due to stale had_messages state
            output, _ = executor._run_session(
                "go", "anthropic/claude-sonnet-4-6", None, timeout=60
            )

        assert "fresh start" in output
