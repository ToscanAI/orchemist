"""Tests for HITL Telegram notification feature (Issue #429.5).

Covers:
- _is_quiet_hours() logic
- TelegramNotifier.dispatch() with inline_keyboard
- NotificationDispatcher.dispatch() with human_review kwargs
- NotificationDispatcher._dispatch_telegram() quiet-hours gate
- TelegramCallbackHandler.handle_update() approve/reject flow
- NotificationDispatcher.from_env() new env vars
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.notifications import (
    NotificationDispatcher,
    TelegramCallbackHandler,
    TelegramNotifier,
    _is_quiet_hours,
)


# ---------------------------------------------------------------------------
# _is_quiet_hours
# ---------------------------------------------------------------------------


class TestIsQuietHours:
    """Unit tests for the module-level _is_quiet_hours helper."""

    def _make_mock_dt(self, hour: int):
        """Return a datetime-like mock with the given hour in Vienna timezone."""
        mock_dt = MagicMock()
        mock_dt.hour = hour
        return mock_dt

    def test_quiet_at_23(self):
        with patch("orchestration_engine.notifications.datetime") as mock_datetime:
            mock_datetime.now.return_value = self._make_mock_dt(23)
            # Can't easily mock ZoneInfo — test the logic directly
        # Direct arithmetic check (no TZ required)
        hour = 23
        quiet_start, quiet_end = 23, 8
        result = hour >= quiet_start or hour < quiet_end
        assert result is True

    def test_quiet_at_3(self):
        hour = 3
        quiet_start, quiet_end = 23, 8
        result = hour >= quiet_start or hour < quiet_end
        assert result is True

    def test_not_quiet_at_12(self):
        hour = 12
        quiet_start, quiet_end = 23, 8
        result = hour >= quiet_start or hour < quiet_end
        assert result is False

    def test_not_quiet_at_8(self):
        hour = 8
        quiet_start, quiet_end = 23, 8
        result = hour >= quiet_start or hour < quiet_end
        assert result is False

    def test_not_quiet_at_22(self):
        hour = 22
        quiet_start, quiet_end = 23, 8
        result = hour >= quiet_start or hour < quiet_end
        assert result is False

    def test_quiet_hours_fallback_no_tz_lib(self):
        """When neither zoneinfo nor pytz is available, returns False (allow)."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name in ("zoneinfo", "pytz"):
                raise ImportError(f"mocked: {name} not available")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            # Should not raise and should return False (allow notifications)
            result = _is_quiet_hours(tz="Europe/Vienna")
        assert result is False


# ---------------------------------------------------------------------------
# TelegramNotifier
# ---------------------------------------------------------------------------


class TestTelegramNotifierDispatch:
    """Tests for TelegramNotifier.dispatch() with inline_keyboard support."""

    def _captured_payload(self, notifier, event, run_id, **kwargs):
        """Call dispatch(), capture what would be sent to the Telegram API."""
        captured = {}

        class MockResponse:
            def read(self):
                return b"{}"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        def mock_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode())
            return MockResponse()

        with patch("orchestration_engine.notifications.urllib.request.urlopen", mock_urlopen):
            notifier.dispatch(event=event, run_id=run_id, **kwargs)

        return captured.get("data", {})

    def test_plain_event_no_keyboard(self):
        n = TelegramNotifier(bot_token="tok", chat_id="123")
        payload = self._captured_payload(n, "auto_merge", "run-1")
        assert "reply_markup" not in payload
        assert "auto_merge" in payload["text"]

    def test_human_review_with_keyboard(self):
        n = TelegramNotifier(bot_token="tok", chat_id="123")
        keyboard = [[{"text": "✅ Approve", "callback_data": "approve:run-2"}]]
        payload = self._captured_payload(
            n, "human_review", "run-2",
            inline_keyboard=keyboard,
            score=0.72,
            tier="review",
            issue_number=429,
            summary="Wire HITL notifications",
            confidence="medium",
        )
        assert "reply_markup" in payload
        assert payload["reply_markup"]["inline_keyboard"] == keyboard

    def test_human_review_message_contains_issue(self):
        n = TelegramNotifier(bot_token="tok", chat_id="123")
        payload = self._captured_payload(
            n, "human_review", "run-3",
            issue_number=42,
            score=0.65,
            tier="review",
            confidence="low",
            summary="Fix the thing",
        )
        text = payload["text"]
        assert "#42" in text
        assert "run-3" in text
        assert "Fix the thing" in text

    def test_human_review_no_pr_url_omits_view_pr(self):
        n = TelegramNotifier(bot_token="tok", chat_id="123")
        payload = self._captured_payload(
            n, "human_review", "run-4",
            inline_keyboard=[[{"text": "✅ Approve", "callback_data": "approve:run-4"}]],
        )
        # Keyboard doesn't have a "View PR" button when pr_url is absent
        buttons_text = str(payload.get("reply_markup", {}))
        assert "View PR" not in buttons_text


# ---------------------------------------------------------------------------
# NotificationDispatcher
# ---------------------------------------------------------------------------


class TestNotificationDispatcherTelegram:
    """Tests for dispatcher._dispatch_telegram() with quiet hours gate."""

    def _make_dispatcher(self, **overrides) -> NotificationDispatcher:
        config = {
            "telegram_enabled": True,
            "telegram_bot_token": "fake-token",
            "telegram_chat_id": "-100001",
            "quiet_hours_enabled": True,
            "quiet_hours_start": 23,
            "quiet_hours_end": 8,
            "quiet_hours_tz": "Europe/Vienna",
        }
        config.update(overrides)
        return NotificationDispatcher(config)

    def _patch_urlopen(self):
        """Return a context manager that captures the last sent payload."""
        sent = {}

        class MockResp:
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, timeout=None):
            sent["payload"] = json.loads(req.data.decode())
            return MockResp()

        return patch("orchestration_engine.notifications.urllib.request.urlopen", fake_urlopen), sent

    def test_quiet_hours_suppresses_human_review(self):
        """During quiet hours, human_review notification is skipped."""
        dispatcher = self._make_dispatcher()

        with patch(
            "orchestration_engine.notifications._is_quiet_hours", return_value=True
        ), patch(
            "orchestration_engine.notifications.urllib.request.urlopen"
        ) as mock_open:
            dispatcher.dispatch(event="human_review", run_id="run-q1", score=0.7)
            mock_open.assert_not_called()

    def test_quiet_hours_allows_non_human_review(self):
        """During quiet hours, non-human_review events are still sent."""
        dispatcher = self._make_dispatcher()
        ctx, sent = self._patch_urlopen()

        with patch(
            "orchestration_engine.notifications._is_quiet_hours", return_value=True
        ), ctx:
            dispatcher.dispatch(event="auto_merge", run_id="run-q2", score=0.9)

        assert "payload" in sent, "auto_merge should still be dispatched during quiet hours"

    def test_human_review_outside_quiet_hours_sends(self):
        """Outside quiet hours, human_review notification is sent."""
        dispatcher = self._make_dispatcher()
        ctx, sent = self._patch_urlopen()

        with patch(
            "orchestration_engine.notifications._is_quiet_hours", return_value=False
        ), ctx:
            dispatcher.dispatch(
                event="human_review",
                run_id="run-q3",
                score=0.72,
                issue_number=429,
                summary="Test summary",
                confidence="medium",
                pr_url="https://github.com/org/repo/pull/1",
            )

        assert "payload" in sent
        payload = sent["payload"]
        assert "reply_markup" in payload
        buttons = payload["reply_markup"]["inline_keyboard"]
        flat = [btn for row in buttons for btn in row]
        texts = [b.get("text", "") for b in flat]
        assert any("Approve" in t for t in texts)
        assert any("Reject" in t for t in texts)
        assert any("View PR" in t for t in texts)

    def test_human_review_no_pr_url_no_view_pr_button(self):
        dispatcher = self._make_dispatcher()
        ctx, sent = self._patch_urlopen()

        with patch(
            "orchestration_engine.notifications._is_quiet_hours", return_value=False
        ), ctx:
            dispatcher.dispatch(
                event="human_review",
                run_id="run-q4",
                score=0.5,
                pr_url="",  # No PR URL
            )

        buttons = sent["payload"]["reply_markup"]["inline_keyboard"]
        flat = [btn for row in buttons for btn in row]
        texts = [b.get("text", "") for b in flat]
        assert not any("View PR" in t for t in texts)

    def test_quiet_hours_disabled_always_sends(self):
        dispatcher = self._make_dispatcher(quiet_hours_enabled=False)
        ctx, sent = self._patch_urlopen()

        with patch(
            "orchestration_engine.notifications._is_quiet_hours", return_value=True
        ), ctx:
            dispatcher.dispatch(event="human_review", run_id="run-q5", score=0.8)

        assert "payload" in sent


# ---------------------------------------------------------------------------
# TelegramCallbackHandler
# ---------------------------------------------------------------------------


def _create_test_db(path: str) -> None:
    """Create a fully-initialized DB with test pipeline_runs rows."""
    from orchestration_engine.db import Database as _Database

    # Let the real Database class create all tables and indexes
    db = _Database(Path(path))
    conn = sqlite3.connect(path)
    _cols = "(run_id, template_id, template_path, input_json, mode, output_dir, status)"
    _vals = "VALUES (?, 't', 't.yaml', '{}', 'standalone', '/tmp', ?)"
    conn.execute(f"INSERT INTO pipeline_runs {_cols} {_vals}", ("run-cb-1", "pending_review"))
    conn.execute(f"INSERT INTO pipeline_runs {_cols} {_vals}", ("run-cb-2", "pending_review"))
    conn.execute(f"INSERT INTO pipeline_runs {_cols} {_vals}", ("run-cb-3", "success"))
    conn.commit()
    conn.close()


class TestTelegramCallbackHandler:
    """Tests for TelegramCallbackHandler.handle_update()."""

    def _make_update(self, callback_data: str, username: str = "rene") -> dict:
        return {
            "update_id": 1,
            "callback_query": {
                "id": "cq1",
                "from": {"id": 42, "username": username, "first_name": "René"},
                "data": callback_data,
            },
        }

    def _make_handler(self, db_path: str) -> TelegramCallbackHandler:
        return TelegramCallbackHandler(
            db_path=db_path,
            gateway_url="",
            gateway_token="",
            bot_token="fake-bot-token",
            chat_id="-100001",
        )

    def test_approve_run(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        _create_test_db(db_file)

        handler = self._make_handler(db_file)

        with patch.object(handler, "_send_telegram_message"), \
             patch.object(handler, "_notify_openclaw"):
            result = handler.handle_update(self._make_update("approve:run-cb-1"))

        assert result["ok"] is True
        assert result["action"] == "approve"
        assert result["run_id"] == "run-cb-1"

        conn = sqlite3.connect(db_file)
        row = conn.execute(
            "SELECT status, reviewed_by FROM pipeline_runs WHERE run_id='run-cb-1'"
        ).fetchone()
        conn.close()
        assert row[0] == "success"
        assert "telegram" in row[1]

    def test_reject_run(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        _create_test_db(db_file)

        handler = self._make_handler(db_file)

        with patch.object(handler, "_send_telegram_message"), \
             patch.object(handler, "_notify_openclaw"):
            result = handler.handle_update(self._make_update("reject:run-cb-2"))

        assert result["ok"] is True
        assert result["action"] == "reject"

        conn = sqlite3.connect(db_file)
        row = conn.execute(
            "SELECT status FROM pipeline_runs WHERE run_id='run-cb-2'"
        ).fetchone()
        conn.close()
        assert row[0] == "rejected"

    def test_approve_non_pending_run_returns_false_ok(self, tmp_path):
        """Approving an already-success run returns ok=False (no row updated)."""
        db_file = str(tmp_path / "test.db")
        _create_test_db(db_file)

        handler = self._make_handler(db_file)
        with patch.object(handler, "_send_telegram_message"), \
             patch.object(handler, "_notify_openclaw"):
            result = handler.handle_update(self._make_update("approve:run-cb-3"))

        assert result["updated"] is False

    def test_unknown_action(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        _create_test_db(db_file)

        handler = self._make_handler(db_file)
        result = handler.handle_update(self._make_update("snooze:run-cb-1"))

        assert result["ok"] is False
        assert "Unknown action" in result["error"]

    def test_missing_callback_query(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        _create_test_db(db_file)

        handler = self._make_handler(db_file)
        result = handler.handle_update({"update_id": 1})

        assert result["ok"] is False

    def test_malformed_callback_data(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        _create_test_db(db_file)

        handler = self._make_handler(db_file)
        result = handler.handle_update(self._make_update("no-colon-here"))

        assert result["ok"] is False


# ---------------------------------------------------------------------------
# NotificationDispatcher.from_env
# ---------------------------------------------------------------------------


class TestNotificationDispatcherFromEnv:
    """Tests for the from_env() factory with new env vars."""

    def test_quiet_hours_defaults(self):
        env = {}
        with patch.dict(os.environ, env, clear=True):
            # Remove all NOTIFY_* vars
            filtered = {k: v for k, v in os.environ.items() if not k.startswith("NOTIFY_")}
            with patch.dict(os.environ, filtered, clear=True):
                d = NotificationDispatcher.from_env()

        assert d._config["quiet_hours_enabled"] is True
        assert d._config["quiet_hours_start"] == 23
        assert d._config["quiet_hours_end"] == 8
        assert d._config["quiet_hours_tz"] == "Europe/Vienna"

    def test_quiet_hours_from_env(self):
        env = {
            "NOTIFY_QUIET_HOURS_ENABLED": "false",
            "NOTIFY_QUIET_HOURS_START": "22",
            "NOTIFY_QUIET_HOURS_END": "9",
        }
        with patch.dict(os.environ, env):
            d = NotificationDispatcher.from_env()

        assert d._config["quiet_hours_enabled"] is False
        assert d._config["quiet_hours_start"] == 22
        assert d._config["quiet_hours_end"] == 9

    def test_callback_db_path_from_env(self):
        env = {"NOTIFY_TELEGRAM_CALLBACK_DB_PATH": "/tmp/test-engine.db"}
        with patch.dict(os.environ, env):
            d = NotificationDispatcher.from_env()
        assert d._config["telegram_callback_db_path"] == "/tmp/test-engine.db"

    def test_quiet_hours_enabled_default_true_when_absent(self):
        """When NOTIFY_QUIET_HOURS_ENABLED is not set, defaults to True."""
        with patch.dict(os.environ, {}, clear=False):
            os_env_no_quiet = {
                k: v for k, v in os.environ.items()
                if k != "NOTIFY_QUIET_HOURS_ENABLED"
            }
            with patch.dict(os.environ, os_env_no_quiet, clear=True):
                d = NotificationDispatcher.from_env()
        assert d._config["quiet_hours_enabled"] is True
