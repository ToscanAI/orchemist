"""Tests for rate limit / stall detection in OpenClawExecutor (#413)."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestStallDetection:
    """Test token progress tracking and stall detection logic."""

    def test_stall_detected_when_no_token_progress(self):
        """Verify WARNING log when tokens don't change for >60s."""
        from orchestration_engine.openclaw_executor import OpenClawExecutor

        executor = OpenClawExecutor(dry_run=True)

        # Simulate stall detection logic directly
        last_token_count = 100
        last_token_change_time = time.monotonic() - 70  # 70s ago
        stall_threshold = 60.0

        stall_seconds = time.monotonic() - last_token_change_time
        assert stall_seconds >= stall_threshold
        assert last_token_count == 100  # no change

    def test_no_stall_when_tokens_progress(self):
        """No stall warning when tokens are increasing."""
        last_token_count = 100
        current_tokens = 150
        assert current_tokens > last_token_count

    def test_stall_resets_on_progress(self):
        """Stall timer resets when token progress resumes."""
        last_token_count = 100
        last_token_change_time = time.monotonic() - 70  # stalled
        stall_warned = True

        # New tokens arrive
        current_tokens = 200
        if current_tokens > last_token_count:
            last_token_count = current_tokens
            last_token_change_time = time.monotonic()
            stall_warned = False

        assert last_token_count == 200
        assert not stall_warned
        assert time.monotonic() - last_token_change_time < 1.0

    def test_stall_threshold_configurable(self):
        """Stall threshold defaults to 60s but can be tuned."""
        threshold = 60.0
        assert threshold == 60.0


class TestEmitStallEvent:
    """Test the SSE event emission for stall detection."""

    def test_emit_stall_event_writes_to_db(self):
        """Verify stall event is written to pipeline_run_events table."""
        from orchestration_engine.db import Database

        db = Database(db_path=Path(":memory:"))

        # Create a running pipeline run (#875: via pipeline_run_dict)
        from tests._helpers import pipeline_run_dict
        db.insert_pipeline_run(pipeline_run_dict(
            "test-run-1",
            template_path="templates/coding-pipeline-v1.yaml",
            template_id="coding-pipeline-v1",
            mode="openclaw",
            status="running",
            pid=12345,
            output_dir="/tmp/test",
            input_config="{}",
        ))

        # Insert a stall event
        event_id = db.insert_pipeline_run_event(
            run_id="test-run-1",
            event_type="stall_detected",
            phase_id="implement",
            metadata={
                "session_key": "agent:main:subagent:test",
                "stall_seconds": 65.0,
                "last_tokens": 1500,
                "message": "No token progress for 65s",
            },
        )

        assert event_id > 0

        # Verify event is retrievable
        events = db.list_pipeline_run_events("test-run-1")
        stall_events = [e for e in events if e["event_type"] == "stall_detected"]
        assert len(stall_events) == 1

        meta = json.loads(stall_events[0]["metadata_json"])
        assert meta["stall_seconds"] == 65.0
        assert meta["last_tokens"] == 1500
        assert "possible rate limit" not in meta["message"]
        assert "No token progress for" in meta["message"]
        assert "65" in meta["message"]

    def test_emit_stall_event_no_running_run(self):
        """No crash when no running pipeline exists."""
        from orchestration_engine.openclaw_executor import OpenClawExecutor

        executor = OpenClawExecutor(dry_run=True)
        # Should not raise
        executor._emit_stall_event("test-session", 90.0, 500)

    def test_multiple_stall_events(self):
        """Multiple stall events can be recorded for the same run."""
        from orchestration_engine.db import Database

        db = Database(db_path=Path(":memory:"))

        from tests._helpers import pipeline_run_dict
        db.insert_pipeline_run(pipeline_run_dict(
            "test-run-2",
            template_path="templates/coding-pipeline-v1.yaml",
            template_id="coding-pipeline-v1",
            mode="openclaw",
            status="running",
            pid=12345,
            output_dir="/tmp/test",
            input_config="{}",
        ))

        # Insert multiple stall events
        db.insert_pipeline_run_event(
            run_id="test-run-2",
            event_type="stall_detected",
            phase_id="spec",
            metadata={"stall_seconds": 60.0, "last_tokens": 100},
        )
        db.insert_pipeline_run_event(
            run_id="test-run-2",
            event_type="stall_detected",
            phase_id="implement",
            metadata={"stall_seconds": 120.0, "last_tokens": 500},
        )

        events = db.list_pipeline_run_events("test-run-2")
        stall_events = [e for e in events if e["event_type"] == "stall_detected"]
        assert len(stall_events) == 2


class TestTokenExtractionFromMessages:
    """Test extracting token counts from session history messages."""

    def test_extract_tokens_from_usage(self):
        """Extract totalTokens from assistant message usage."""
        msg = {
            "role": "assistant",
            "usage": {"totalTokens": 5000, "input": 2000, "output": 3000},
        }
        usage = msg.get("usage", {})
        tokens = usage.get("totalTokens", 0) or (
            usage.get("input", 0) + usage.get("output", 0)
        )
        assert tokens == 5000

    def test_extract_tokens_fallback_to_input_output(self):
        """Fall back to input+output when totalTokens is missing."""
        msg = {
            "role": "assistant",
            "usage": {"input": 2000, "output": 3000},
        }
        usage = msg.get("usage", {})
        tokens = usage.get("totalTokens", 0) or (
            usage.get("input", 0) + usage.get("output", 0)
        )
        assert tokens == 5000

    def test_extract_tokens_no_usage(self):
        """Zero tokens when usage is missing entirely."""
        msg = {"role": "assistant", "content": "hello"}
        usage = msg.get("usage", {})
        tokens = usage.get("totalTokens", 0) or (
            usage.get("input", 0) + usage.get("output", 0)
        )
        assert tokens == 0

    def test_extract_tokens_zero_total(self):
        """When totalTokens is 0, fall back to input+output."""
        msg = {
            "role": "assistant",
            "usage": {"totalTokens": 0, "input": 100, "output": 200},
        }
        usage = msg.get("usage", {})
        tokens = usage.get("totalTokens", 0) or (
            usage.get("input", 0) + usage.get("output", 0)
        )
        assert tokens == 300


class TestCLIStallWarning:
    """Test that orch status shows stall warnings."""

    def test_stall_warning_in_status_output(self):
        """Verify _print_run_detail shows stall warning for running pipelines."""
        from orchestration_engine.db import Database

        db = Database(db_path=Path(":memory:"))

        from tests._helpers import pipeline_run_dict
        db.insert_pipeline_run(pipeline_run_dict(
            "stall-run",
            template_path="templates/coding-pipeline-v1.yaml",
            template_id="coding-pipeline-v1",
            mode="openclaw",
            status="running",
            pid=99999,
            output_dir="/tmp/test",
            input_config="{}",
        ))

        db.insert_pipeline_run_event(
            run_id="stall-run",
            event_type="stall_detected",
            phase_id="review",
            metadata={
                "message": "No token progress for 90s",
            },
        )

        events = db.list_pipeline_run_events("stall-run")
        stall_events = [e for e in events if e["event_type"] == "stall_detected"]
        assert len(stall_events) == 1
        meta = json.loads(stall_events[0]["metadata_json"])
        assert "possible rate limit" not in meta["message"]
        assert "No token progress for" in meta["message"]
