"""Tests for Issue #331.4 — HITL Review Queue API & Notification Dispatcher.

Covers:
- DB migration 008 adds review columns to pipeline_runs
- list_pending_reviews returns enriched rows with routing data
- count_pending_reviews returns correct count
- approve_pipeline_run transitions status to 'success'
- reject_pipeline_run transitions status to 'rejected'
- NotificationDispatcher.from_env builds correct config
- NotificationDispatcher.dispatch emits log notification
- Webhook and OpenClaw backends are non-fatal on failure
- daemon.py _dispatch_routing_action dispatches notification for human_review
- REST API GET /api/v1/reviews returns pending runs
- REST API POST /api/v1/reviews/{run_id}/approve
- REST API POST /api/v1/reviews/{run_id}/reject
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.db import Database
from orchestration_engine.notifications import NotificationDispatcher


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Database:
    """Return a fresh in-memory Database (all migrations applied)."""
    return Database(Path(":memory:"))


def _seed_run(
    db: Database,
    run_id: str,
    status: str = "pending_review",
    tmp_path: Path = Path("/tmp"),
) -> None:
    """Insert a minimal pipeline_runs row."""
    db.insert_pipeline_run(
        {
            "run_id": run_id,
            "template_path": "/tmp/fake.yaml",
            "template_id": "fake-template",
            "input_json": "{}",
            "mode": "dry-run",
            "output_dir": str(tmp_path),
            "status": status,
        }
    )
    if status != "pending":
        db.update_pipeline_run(run_id, status=status)


def _seed_routing_decision(db: Database, run_id: str, score: float = 0.72) -> None:
    """Insert a routing decision row for an existing run."""
    db.insert_routing_decision(
        {
            "run_id": run_id,
            "confidence_score": score,
            "tier_name": "needs_review",
            "action": "human_review",
            "justification": "Score below auto-merge threshold.",
            "signals_json": "{}",
        }
    )


# ===========================================================================
# 1.  Database — migration & new methods
# ===========================================================================


class TestMigration008:
    """Migration 008 adds review columns to pipeline_runs."""

    def test_review_columns_present_after_migration(self, tmp_path):
        """Fresh DB should have review_reason, reviewed_at, reviewed_by."""
        db = _make_db(tmp_path)
        conn = db.get_connection()
        cursor = conn.execute("PRAGMA table_info(pipeline_runs)")
        col_names = {row[1] for row in cursor.fetchall()}
        assert "review_reason" in col_names
        assert "reviewed_at" in col_names
        assert "reviewed_by" in col_names

    def test_review_columns_default_null(self, tmp_path):
        """review_reason, reviewed_at, reviewed_by default to NULL."""
        db = _make_db(tmp_path)
        _seed_run(db, "run-mig-001", status="pending_review")
        run = db.get_pipeline_run("run-mig-001")
        assert run["review_reason"] is None
        assert run["reviewed_at"] is None
        assert run["reviewed_by"] is None


class TestListPendingReviews:
    """list_pending_reviews returns enriched pending_review rows."""

    def test_empty_queue(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.list_pending_reviews() == []

    def test_returns_pending_review_runs(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-pr-001", status="pending_review")
        _seed_run(db, "run-pr-002", status="pending_review")
        _seed_run(db, "run-ok-003", status="success")  # should be excluded

        items = db.list_pending_reviews()
        run_ids = {r["run_id"] for r in items}
        assert "run-pr-001" in run_ids
        assert "run-pr-002" in run_ids
        assert "run-ok-003" not in run_ids

    def test_enriched_with_routing_data(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-enrich-001", status="pending_review")
        _seed_routing_decision(db, "run-enrich-001", score=0.65)

        items = db.list_pending_reviews()
        assert len(items) == 1
        item = items[0]
        assert item["confidence_score"] == pytest.approx(0.65, abs=1e-6)
        assert item["tier_name"] == "needs_review"
        assert item["action"] == "human_review"
        assert "threshold" in item["justification"] or "Score" in item["justification"]

    def test_routing_columns_none_when_no_decision(self, tmp_path):
        """Runs with no routing decision should still appear with NULL routing cols."""
        db = _make_db(tmp_path)
        _seed_run(db, "run-no-rd-001", status="pending_review")

        items = db.list_pending_reviews()
        assert len(items) == 1
        assert items[0]["confidence_score"] is None
        assert items[0]["tier_name"] is None

    def test_pagination(self, tmp_path):
        db = _make_db(tmp_path)
        for i in range(5):
            _seed_run(db, f"run-page-{i:03d}", status="pending_review")

        page1 = db.list_pending_reviews(limit=2, offset=0)
        page2 = db.list_pending_reviews(limit=2, offset=2)
        page3 = db.list_pending_reviews(limit=2, offset=4)

        assert len(page1) == 2
        assert len(page2) == 2
        assert len(page3) == 1  # only one left

        all_ids = {r["run_id"] for r in page1 + page2 + page3}
        assert len(all_ids) == 5  # no duplicates


class TestCountPendingReviews:
    """count_pending_reviews returns correct integer."""

    def test_empty(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.count_pending_reviews() == 0

    def test_counts_only_pending_review(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-cnt-001", status="pending_review")
        _seed_run(db, "run-cnt-002", status="pending_review")
        _seed_run(db, "run-cnt-003", status="success")
        _seed_run(db, "run-cnt-004", status="failed")

        assert db.count_pending_reviews() == 2


class TestApprovePipelineRun:
    """approve_pipeline_run transitions pending_review → success."""

    def test_approve_sets_status_success(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-approve-001", status="pending_review")

        result = db.approve_pipeline_run("run-approve-001")
        assert result is True

        run = db.get_pipeline_run("run-approve-001")
        assert run["status"] == "success"

    def test_approve_stores_reviewed_by(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-approve-002", status="pending_review")

        db.approve_pipeline_run("run-approve-002", reviewed_by="operator@example.com")

        run = db.get_pipeline_run("run-approve-002")
        assert run["reviewed_by"] == "operator@example.com"

    def test_approve_stores_note(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-approve-003", status="pending_review")

        db.approve_pipeline_run("run-approve-003", note="LGTM")

        run = db.get_pipeline_run("run-approve-003")
        assert run["review_reason"] == "LGTM"

    def test_approve_stores_reviewed_at(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-approve-004", status="pending_review")

        db.approve_pipeline_run("run-approve-004")

        run = db.get_pipeline_run("run-approve-004")
        assert run["reviewed_at"] is not None

    def test_approve_returns_false_for_nonexistent_run(self, tmp_path):
        db = _make_db(tmp_path)
        result = db.approve_pipeline_run("run-does-not-exist")
        assert result is False

    def test_approve_returns_false_for_non_pending_status(self, tmp_path):
        """Approving an already-approved run is a no-op."""
        db = _make_db(tmp_path)
        _seed_run(db, "run-approve-done", status="success")

        result = db.approve_pipeline_run("run-approve-done")
        assert result is False


class TestRejectPipelineRun:
    """reject_pipeline_run transitions pending_review → rejected."""

    def test_reject_sets_status_rejected(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-reject-001", status="pending_review")

        result = db.reject_pipeline_run("run-reject-001", reason="Quality too low")
        assert result is True

        run = db.get_pipeline_run("run-reject-001")
        assert run["status"] == "rejected"

    def test_reject_stores_reason(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-reject-002", status="pending_review")

        db.reject_pipeline_run("run-reject-002", reason="Rubric score below threshold")

        run = db.get_pipeline_run("run-reject-002")
        assert run["review_reason"] == "Rubric score below threshold"

    def test_reject_stores_reviewed_by(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-reject-003", status="pending_review")

        db.reject_pipeline_run("run-reject-003", reason="Bad output", reviewed_by="qa@example.com")

        run = db.get_pipeline_run("run-reject-003")
        assert run["reviewed_by"] == "qa@example.com"

    def test_reject_stores_reviewed_at(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-reject-004", status="pending_review")

        db.reject_pipeline_run("run-reject-004", reason="Rejected")

        run = db.get_pipeline_run("run-reject-004")
        assert run["reviewed_at"] is not None

    def test_reject_returns_false_for_nonexistent_run(self, tmp_path):
        db = _make_db(tmp_path)
        result = db.reject_pipeline_run("run-does-not-exist", reason="N/A")
        assert result is False

    def test_reject_returns_false_for_non_pending_status(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_run(db, "run-already-rejected", status="rejected")

        result = db.reject_pipeline_run("run-already-rejected", reason="Again?")
        assert result is False


# ===========================================================================
# 2.  NotificationDispatcher
# ===========================================================================


class TestNotificationDispatcherFromEnv:
    """from_env reads config correctly from environment variables."""

    def test_defaults_all_disabled(self, monkeypatch):
        """Without env vars all backends are disabled."""
        for var in (
            "NOTIFY_OPENCLAW_ENABLED",
            "NOTIFY_OPENCLAW_GATEWAY_URL",
            "NOTIFY_OPENCLAW_GATEWAY_TOKEN",
            "NOTIFY_OPENCLAW_SESSION",
            "NOTIFY_WEBHOOK_ENABLED",
            "NOTIFY_WEBHOOK_URL",
            "NOTIFY_WEBHOOK_SECRET",
        ):
            monkeypatch.delenv(var, raising=False)

        d = NotificationDispatcher.from_env()
        assert d._config["openclaw_enabled"] is False
        assert d._config["webhook_enabled"] is False

    def test_openclaw_enabled_flag(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_OPENCLAW_ENABLED", "1")
        d = NotificationDispatcher.from_env()
        assert d._config["openclaw_enabled"] is True

    def test_webhook_enabled_flag(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_WEBHOOK_ENABLED", "true")
        d = NotificationDispatcher.from_env()
        assert d._config["webhook_enabled"] is True

    def test_openclaw_session_default(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_OPENCLAW_SESSION", raising=False)
        d = NotificationDispatcher.from_env()
        assert d._config["openclaw_session"] == "agent:main:main"

    def test_openclaw_gateway_url_from_fallback(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_OPENCLAW_GATEWAY_URL", raising=False)
        monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://localhost:18789")
        d = NotificationDispatcher.from_env()
        assert d._config["openclaw_gateway_url"] == "http://localhost:18789"

    def test_webhook_url_populated(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.com/review")
        d = NotificationDispatcher.from_env()
        assert d._config["webhook_url"] == "https://hooks.example.com/review"


class TestNotificationDispatcherDispatch:
    """dispatch() logs and optionally invokes backends."""

    def test_dispatch_emits_warning_log(self, caplog):
        d = NotificationDispatcher()
        with caplog.at_level(logging.WARNING, logger="orchestration_engine.notifications"):
            d.dispatch(
                event="human_review",
                run_id="run-log-001",
                tier="needs_review",
                score=0.63,
                justification="Low test coverage",
            )
        assert "run-log-001" in caplog.text
        assert "human_review" in caplog.text

    def test_dispatch_does_not_raise_without_backends(self):
        """Dispatch with no backends configured must not raise."""
        d = NotificationDispatcher()
        d.dispatch(event="human_review", run_id="run-safe-001")

    def test_dispatch_calls_openclaw_when_enabled(self):
        """openclaw backend is called when configured."""
        config = {
            "openclaw_enabled": True,
            "openclaw_gateway_url": "http://localhost:18789",
            "openclaw_gateway_token": "token123",
            "openclaw_session": "agent:main:main",
            "webhook_enabled": False,
        }
        d = NotificationDispatcher(config)

        with patch.object(d, "_dispatch_openclaw") as mock_oc:
            d.dispatch(event="human_review", run_id="run-oc-001", score=0.55)
            mock_oc.assert_called_once()

    def test_dispatch_calls_webhook_when_enabled(self):
        """webhook backend is called when configured."""
        config = {
            "openclaw_enabled": False,
            "webhook_enabled": True,
            "webhook_url": "https://hooks.example.com/review",
            "webhook_secret": "",
        }
        d = NotificationDispatcher(config)

        with patch.object(d, "_dispatch_webhook") as mock_wh:
            d.dispatch(event="human_review", run_id="run-wh-001", score=0.60)
            mock_wh.assert_called_once()

    def test_dispatch_openclaw_failure_is_non_fatal(self):
        """Exceptions in _dispatch_openclaw must not propagate."""
        config = {"openclaw_enabled": True, "openclaw_gateway_url": "http://bad-host", "openclaw_gateway_token": ""}
        d = NotificationDispatcher(config)
        # Should not raise even with an unreachable host
        d.dispatch(event="human_review", run_id="run-oc-fail-001")

    def test_dispatch_webhook_failure_is_non_fatal(self):
        """Exceptions in _dispatch_webhook must not propagate."""
        config = {"webhook_enabled": True, "webhook_url": "http://bad-host-webhook"}
        d = NotificationDispatcher(config)
        d.dispatch(event="human_review", run_id="run-wh-fail-001")


# ===========================================================================
# 3.  Daemon — notification dispatch integration
# ===========================================================================


class TestDaemonNotificationDispatch:
    """_dispatch_routing_action triggers notification for human_review."""

    def test_notification_dispatch_called_on_human_review(self):
        """NotificationDispatcher.dispatch is called inside human_review branch.

        The import is lazy (inside the function body), so we patch the module
        where ``NotificationDispatcher`` lives.
        """
        from orchestration_engine.daemon import _dispatch_routing_action
        import orchestration_engine.notifications as _notif_module

        decision = MagicMock()
        decision.tier = "needs_review"
        decision.score = 0.72

        confidence_result = MagicMock()
        confidence_result.explanation = "Score below auto-merge threshold."

        mock_dispatcher = MagicMock()
        mock_dispatcher_cls = MagicMock(return_value=mock_dispatcher)
        mock_dispatcher_cls.from_env = MagicMock(return_value=mock_dispatcher)

        with patch.object(_notif_module, "NotificationDispatcher", mock_dispatcher_cls):
            _dispatch_routing_action(
                run_id="run-daemon-001",
                action="human_review",
                decision=decision,
                confidence_result=confidence_result,
                auto_merge_config=None,
                phase_outputs={},
            )
            mock_dispatcher_cls.from_env.assert_called_once()
            mock_dispatcher.dispatch.assert_called_once()
            call_kwargs = mock_dispatcher.dispatch.call_args
            assert call_kwargs.kwargs.get("run_id") == "run-daemon-001" or \
                   (call_kwargs.args and "run-daemon-001" in call_kwargs.args)

    def test_notification_failure_is_non_fatal(self):
        """Notification dispatch errors must not abort the pipeline."""
        from orchestration_engine.daemon import _dispatch_routing_action
        import orchestration_engine.notifications as _notif_module

        decision = MagicMock()
        decision.tier = "needs_review"
        decision.score = 0.72

        confidence_result = MagicMock()

        mock_dispatcher_cls = MagicMock()
        mock_dispatcher_cls.from_env.side_effect = RuntimeError("Cannot connect")

        with patch.object(_notif_module, "NotificationDispatcher", mock_dispatcher_cls):
            # Must not raise
            _dispatch_routing_action(
                run_id="run-daemon-fail-001",
                action="human_review",
                decision=decision,
                confidence_result=confidence_result,
                auto_merge_config=None,
                phase_outputs={},
            )

    def test_auto_merge_does_not_dispatch_notification(self):
        """Notification should NOT be dispatched for auto_merge actions."""
        from orchestration_engine.daemon import _dispatch_routing_action
        import orchestration_engine.notifications as _notif_module

        decision = MagicMock()
        decision.tier = "auto_merge"
        decision.score = 0.95

        confidence_result = MagicMock()

        mock_dispatcher_cls = MagicMock()

        with patch.object(_notif_module, "NotificationDispatcher", mock_dispatcher_cls):
            with patch("orchestration_engine.daemon._dispatch_auto_merge"):
                _dispatch_routing_action(
                    run_id="run-daemon-am-001",
                    action="auto_merge",
                    decision=decision,
                    confidence_result=confidence_result,
                    auto_merge_config=None,
                    phase_outputs={},
                )
            mock_dispatcher_cls.from_env.assert_not_called()


# ===========================================================================
# 4.  REST API — Review Queue endpoints
# ===========================================================================


@pytest.fixture
def api_client(tmp_path):
    """Create a FastAPI test client backed by an in-memory DB."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orchestration_engine.web.api import create_api_app

    db_path = str(tmp_path / "engine.db")
    app = create_api_app(db_path=db_path)
    return TestClient(app), db_path


class TestReviewListEndpoint:
    """GET /api/v1/reviews returns pending_review runs."""

    def test_empty_queue(self, api_client):
        client, _ = api_client
        resp = client.get("/api/v1/reviews")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_returns_pending_runs(self, api_client):
        client, db_path = api_client
        db = Database(Path(db_path))
        _seed_run(db, "run-api-list-001", status="pending_review")
        _seed_run(db, "run-api-list-002", status="success")  # excluded

        resp = client.get("/api/v1/reviews")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["run_id"] == "run-api-list-001"

    def test_pagination_params_respected(self, api_client):
        client, db_path = api_client
        db = Database(Path(db_path))
        for i in range(5):
            _seed_run(db, f"run-api-page-{i:03d}", status="pending_review")

        resp = client.get("/api/v1/reviews?limit=2&offset=2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["total"] == 5
        assert body["limit"] == 2
        assert body["offset"] == 2


class TestReviewApproveEndpoint:
    """POST /api/v1/reviews/{run_id}/approve."""

    def test_approve_returns_200(self, api_client):
        client, db_path = api_client
        db = Database(Path(db_path))
        _seed_run(db, "run-api-approve-001", status="pending_review")

        resp = client.post(
            "/api/v1/reviews/run-api-approve-001/approve",
            json={"reviewed_by": "ops@example.com", "note": "Ship it"},
        )
        assert resp.status_code == 200
        assert resp.json()["approved"] is True

    def test_approve_updates_db_status(self, api_client):
        client, db_path = api_client
        db = Database(Path(db_path))
        _seed_run(db, "run-api-approve-002", status="pending_review")

        client.post("/api/v1/reviews/run-api-approve-002/approve", json={})

        run = db.get_pipeline_run("run-api-approve-002")
        assert run["status"] == "success"

    def test_approve_nonexistent_run_returns_404(self, api_client):
        client, _ = api_client
        resp = client.post("/api/v1/reviews/run-ghost/approve", json={})
        assert resp.status_code == 404

    def test_approve_non_pending_run_returns_404(self, api_client):
        client, db_path = api_client
        db = Database(Path(db_path))
        _seed_run(db, "run-api-approve-done", status="success")

        resp = client.post("/api/v1/reviews/run-api-approve-done/approve", json={})
        assert resp.status_code == 404


class TestReviewRejectEndpoint:
    """POST /api/v1/reviews/{run_id}/reject."""

    def test_reject_returns_200(self, api_client):
        client, db_path = api_client
        db = Database(Path(db_path))
        _seed_run(db, "run-api-reject-001", status="pending_review")

        resp = client.post(
            "/api/v1/reviews/run-api-reject-001/reject",
            json={"reason": "Output quality insufficient", "reviewed_by": "qa@example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["rejected"] is True

    def test_reject_updates_db_status(self, api_client):
        client, db_path = api_client
        db = Database(Path(db_path))
        _seed_run(db, "run-api-reject-002", status="pending_review")

        client.post(
            "/api/v1/reviews/run-api-reject-002/reject",
            json={"reason": "Bad output"},
        )

        run = db.get_pipeline_run("run-api-reject-002")
        assert run["status"] == "rejected"
        assert run["review_reason"] == "Bad output"

    def test_reject_nonexistent_run_returns_404(self, api_client):
        client, _ = api_client
        resp = client.post(
            "/api/v1/reviews/run-ghost/reject",
            json={"reason": "Not found"},
        )
        assert resp.status_code == 404

    def test_reject_non_pending_run_returns_404(self, api_client):
        client, db_path = api_client
        db = Database(Path(db_path))
        _seed_run(db, "run-api-reject-done", status="rejected")

        resp = client.post(
            "/api/v1/reviews/run-api-reject-done/reject",
            json={"reason": "Already rejected"},
        )
        assert resp.status_code == 404

    def test_reject_missing_reason_returns_422(self, api_client):
        """reason field is mandatory; missing it should return 422."""
        client, db_path = api_client
        db = Database(Path(db_path))
        _seed_run(db, "run-api-reject-no-reason", status="pending_review")

        resp = client.post(
            "/api/v1/reviews/run-api-reject-no-reason/reject",
            json={},  # reason omitted
        )
        assert resp.status_code == 422
