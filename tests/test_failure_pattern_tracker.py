"""Tests for FailurePatternTracker and related db.py methods (Issue #3.1.3)."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.diagnosis import (
    FailurePatternTracker,
    _normalise_error,
)
from orchestration_engine.db import Database


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_db(tmp_path: Path) -> Database:
    """Create a fresh in-memory-ish DB under *tmp_path*."""
    return Database(str(tmp_path / "test.db"))


# ---------------------------------------------------------------------------
# _normalise_error
# ---------------------------------------------------------------------------

class TestNormaliseError:
    def test_lowercases(self):
        assert _normalise_error("TIMEOUT") == "timeout"

    def test_strips_uuid(self):
        result = _normalise_error("run 123e4567-e89b-12d3-a456-426614174000 failed")
        assert "<uuid>" in result
        assert "123e4567" not in result

    def test_strips_path(self):
        result = _normalise_error("error reading /home/user/file.txt")
        assert "<path>" in result
        assert "/home/user/file.txt" not in result

    def test_strips_large_integers(self):
        result = _normalise_error("timeout after 600 seconds")
        assert "<int>" in result
        assert "600" not in result

    def test_preserves_short_words(self):
        # single-digit numbers should NOT be stripped (they match \b\d{2,}\b rule)
        result = _normalise_error("phase 1 failed")
        assert "phase" in result

    def test_strips_hex_hash(self):
        result = _normalise_error("git hash deadbeef1234abcd")
        assert "<hex>" in result

    def test_collapses_whitespace(self):
        result = _normalise_error("a   b   c")
        assert result == "a b c"

    def test_same_message_normalises_identically(self):
        # Same root cause, only the path differs → should normalise identically
        msg1 = "Phase 'build' failed at /home/alice/project: file not found"
        msg2 = "Phase 'build' failed at /home/bob/workspace: file not found"
        assert _normalise_error(msg1) == _normalise_error(msg2)


# ---------------------------------------------------------------------------
# Database.insert_or_update_failure_pattern
# ---------------------------------------------------------------------------

class TestInsertOrUpdateFailurePattern:
    def test_inserts_new_pattern(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        record = db.insert_or_update_failure_pattern(
            pattern_hash="abc123",
            template_id="tmpl-1",
            failure_class="timeout",
            now_iso=now,
        )
        assert record["pattern_hash"] == "abc123"
        assert record["template_id"] == "tmpl-1"
        assert record["failure_class"] == "timeout"
        assert record["occurrence_count"] == 1
        assert record["is_systemic"] == 0

    def test_increments_on_duplicate(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        for _ in range(3):
            record = db.insert_or_update_failure_pattern(
                pattern_hash="abc123",
                template_id="tmpl-1",
                failure_class="timeout",
                now_iso=now,
            )
        assert record["occurrence_count"] == 3

    def test_marks_systemic_at_threshold(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        record = None
        for _ in range(3):
            record = db.insert_or_update_failure_pattern(
                pattern_hash="abc123",
                template_id="tmpl-1",
                failure_class="timeout",
                now_iso=now,
                systemic_threshold=3,
                systemic_window_days=7,
            )
        assert record["is_systemic"] == 1

    def test_not_systemic_below_threshold(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        record = None
        for _ in range(2):
            record = db.insert_or_update_failure_pattern(
                pattern_hash="abc123",
                template_id="tmpl-1",
                failure_class="timeout",
                now_iso=now,
                systemic_threshold=3,
                systemic_window_days=7,
            )
        assert record["is_systemic"] == 0

    def test_different_templates_tracked_separately(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        for _ in range(3):
            db.insert_or_update_failure_pattern(
                pattern_hash="abc123",
                template_id="tmpl-A",
                failure_class="timeout",
                now_iso=now,
            )
        # Different template — should be a separate row, count=1
        record_b = db.insert_or_update_failure_pattern(
            pattern_hash="abc123",
            template_id="tmpl-B",
            failure_class="timeout",
            now_iso=now,
        )
        assert record_b["occurrence_count"] == 1
        assert record_b["is_systemic"] == 0


# ---------------------------------------------------------------------------
# Database.get_failure_patterns
# ---------------------------------------------------------------------------

class TestGetFailurePatterns:
    def test_returns_all_when_no_filter(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        db.insert_or_update_failure_pattern("h1", "tmpl-1", "timeout", now)
        db.insert_or_update_failure_pattern("h2", "tmpl-2", "infra_issue", now)
        patterns = db.get_failure_patterns()
        assert len(patterns) == 2

    def test_filters_by_template_id(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        db.insert_or_update_failure_pattern("h1", "tmpl-1", "timeout", now)
        db.insert_or_update_failure_pattern("h2", "tmpl-2", "infra_issue", now)
        patterns = db.get_failure_patterns(template_id="tmpl-1")
        assert len(patterns) == 1
        assert patterns[0]["template_id"] == "tmpl-1"

    def test_systemic_only_filter(self, tmp_path):
        db = make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        # Make one systemic (3 hits)
        for _ in range(3):
            db.insert_or_update_failure_pattern("h1", "tmpl-1", "timeout", now, systemic_threshold=3)
        # Non-systemic
        db.insert_or_update_failure_pattern("h2", "tmpl-1", "infra_issue", now)

        systemic = db.get_failure_patterns(systemic_only=True)
        assert len(systemic) == 1
        assert systemic[0]["pattern_hash"] == "h1"


# ---------------------------------------------------------------------------
# FailurePatternTracker
# ---------------------------------------------------------------------------

class TestFailurePatternTracker:
    def test_track_inserts_record(self, tmp_path):
        db = make_db(tmp_path)
        tracker = FailurePatternTracker(db=db)
        record = tracker.track(
            template_id="coding-pipeline-v1",
            failure_class="timeout",
            error_message="Phase 'build' timed out after 600s",
        )
        assert record["template_id"] == "coding-pipeline-v1"
        assert record["occurrence_count"] == 1

    def test_track_same_message_increments(self, tmp_path):
        db = make_db(tmp_path)
        tracker = FailurePatternTracker(db=db)
        msg = "Phase 'build' timed out after 600s"
        for _ in range(3):
            record = tracker.track("tmpl", "timeout", msg)
        assert record["occurrence_count"] == 3

    def test_track_varied_messages_hash_same(self, tmp_path):
        """Two messages that normalise identically should hash to the same bucket."""
        db = make_db(tmp_path)
        tracker = FailurePatternTracker(db=db)
        # Only the path differs — both should normalise to the same string
        tracker.track("tmpl", "timeout", "Phase 'build' failed at /home/alice/project: file not found")
        record = tracker.track("tmpl", "timeout", "Phase 'build' failed at /home/bob/workspace: file not found")
        # Both normalise identically → same pattern_hash → count=2
        assert record["occurrence_count"] == 2

    def test_systemic_flag_logged(self, tmp_path, caplog):
        db = make_db(tmp_path)
        tracker = FailurePatternTracker(db=db)
        tracker.SYSTEMIC_THRESHOLD = 3
        import logging
        with caplog.at_level(logging.WARNING, logger="orchestration_engine.diagnosis"):
            for _ in range(3):
                tracker.track("tmpl", "timeout", "timeout error")
        assert any("systemic" in r.message.lower() for r in caplog.records)

    def test_no_systemic_log_below_threshold(self, tmp_path, caplog):
        db = make_db(tmp_path)
        tracker = FailurePatternTracker(db=db)
        import logging
        with caplog.at_level(logging.WARNING, logger="orchestration_engine.diagnosis"):
            tracker.track("tmpl", "timeout", "timeout error")
        assert not any("systemic" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# DiagnosisEngine.diagnose wires FailurePatternTracker
# ---------------------------------------------------------------------------

class TestDiagnosisEngineTrackerIntegration:
    def test_diagnose_calls_tracker_when_template_id_provided(self, tmp_path):
        """When template_id is passed, diagnose() should track the failure pattern."""
        db = make_db(tmp_path)

        # Stub executor returning a valid diagnosis JSON
        mock_executor = MagicMock()
        mock_result = MagicMock()
        mock_result.state = "success"
        mock_result.result = {
            "text": '{"failure_class":"timeout","remediation":"retry_same","confidence":0.9,"explanation":"timed out"}'
        }
        mock_result.model_used = "haiku"
        mock_result.tokens_consumed = 100
        mock_executor.execute.return_value = mock_result

        from orchestration_engine.diagnosis import DiagnosisEngine
        import uuid

        run_id = f"run-{uuid.uuid4().hex[:8]}"
        # Insert a placeholder pipeline run so FK constraint is happy
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": "/tmp/fake.yaml",
            "template_id": "coding-pipeline-v1",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": "/tmp/out",
        })

        engine = DiagnosisEngine(executor=mock_executor, db=db)
        engine.diagnose(
            run_id=run_id,
            error_message="Phase 'build' timed out after 600s",
            output_dir=None,
            template_id="coding-pipeline-v1",
        )

        patterns = db.get_failure_patterns(template_id="coding-pipeline-v1")
        assert len(patterns) == 1
        assert patterns[0]["failure_class"] == "timeout"

    def test_diagnose_skips_tracker_when_no_template_id(self, tmp_path):
        """When template_id is None, no failure_patterns row is inserted."""
        db = make_db(tmp_path)

        mock_executor = MagicMock()
        mock_result = MagicMock()
        mock_result.state = "success"
        mock_result.result = {
            "text": '{"failure_class":"timeout","remediation":"retry_same","confidence":0.9,"explanation":"timed out"}'
        }
        mock_result.model_used = "haiku"
        mock_result.tokens_consumed = 100
        mock_executor.execute.return_value = mock_result

        from orchestration_engine.diagnosis import DiagnosisEngine
        import uuid

        run_id = f"run-{uuid.uuid4().hex[:8]}"
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": "/tmp/fake.yaml",
            "template_id": "coding-pipeline-v1",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": "/tmp/out",
        })

        engine = DiagnosisEngine(executor=mock_executor, db=db)
        engine.diagnose(run_id=run_id, error_message="timeout", output_dir=None)
        # No template_id → no patterns
        patterns = db.get_failure_patterns()
        assert len(patterns) == 0
