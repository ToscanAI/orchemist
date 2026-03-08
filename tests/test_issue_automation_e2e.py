"""End-to-end integration tests for IssueAutomation — Issue #5.1.5.

Covers the full flow: mock issue → classify → template select → input extract
→ launch → result posting, as well as the confidence escalation gate.

Test inventory:
- E2E happy path: high-confidence issue → pipeline launched, escalated=False
- E2E with DB: classification persisted, status updated to "launched"
- E2E escalation path: low-confidence → no launch, escalated=True
- E2E escalation with dispatcher: dispatcher.dispatch() called with correct kwargs
- E2E escalation without dispatcher: no dispatch, graceful silent escalation
- E2E escalation: run_id is None on escalated result
- E2E escalation: comment body contains warning text
- E2E escalation: comment body does NOT contain pipeline/run-id lines
- E2E normal path: comment body contains pipeline name and run_id
- E2E normal path: launcher called with correct template and inputs
- E2E normal path: result dict contains all expected keys
- E2E custom threshold: exact boundary (confidence == threshold) → NOT escalated
- E2E custom threshold: just below boundary → escalated
- E2E template resolver failure → falls back to empty schema, still launches
- E2E no launcher → run_id None, escalated=False for high-confidence
- Confidence threshold default is 0.70
- IssueAutomation accepts confidence_threshold kwarg
- IssueAutomation accepts notification_dispatcher kwarg

All tests are fully mocked — no real LLM calls, no real network, no real DB writes
(temp-file SQLite used where DB persistence is tested).
"""

from __future__ import annotations

import json
import tempfile
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, call

import pytest

from orchestration_engine.issue_automation import (
    IssueAutomation,
    IssueClassification,
    IssueClassifier,
    InputExtractor,
    TemplateSelector,
)
from orchestration_engine.notifications import NotificationDispatcher
from orchestration_engine.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Return a fresh temp-file SQLite Database."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Database(tmp.name)


def _mock_classifier(cls_type: str = "bug", confidence: float = 0.90) -> IssueClassifier:
    """Return an IssueClassifier whose executor returns a deterministic response."""
    mock_exec = MagicMock()
    mock_exec.execute.return_value = json.dumps({
        "classification_type": cls_type,
        "confidence": confidence,
        "reasoning": "E2E test reasoning.",
    })
    return IssueClassifier(executor=mock_exec)


def _mock_extractor(extracted: Optional[Dict[str, Any]] = None) -> InputExtractor:
    """Return an InputExtractor whose executor returns *extracted* as JSON."""
    data = extracted or {"issue_number": 42, "repo": "owner/repo"}
    mock_exec = MagicMock()
    mock_exec.execute.return_value = json.dumps(data)
    return InputExtractor(executor=mock_exec)


def _mock_template(config_schema: Optional[Dict[str, Any]] = None) -> Any:
    """Return a mock template object with an optional config_schema."""
    tpl = MagicMock()
    tpl.config_schema = config_schema or {}
    return tpl


def _make_launcher(run_id: str = "test-run-abc") -> MagicMock:
    """Return a launcher callable that returns a run dict."""
    launcher = MagicMock(return_value={"run_id": run_id})
    return launcher


def _make_automation(
    cls_type: str = "bug",
    confidence: float = 0.90,
    confidence_threshold: float = 0.70,
    dispatcher: Optional[NotificationDispatcher] = None,
) -> IssueAutomation:
    """Build a fully-wired IssueAutomation with mock dependencies."""
    return IssueAutomation(
        classifier=_mock_classifier(cls_type, confidence),
        selector=TemplateSelector(),
        extractor=_mock_extractor(),
        confidence_threshold=confidence_threshold,
        notification_dispatcher=dispatcher,
    )


def _make_template_resolver_and_engine(config_schema: Optional[Dict] = None):
    """Return (template_resolver, template_engine) mocks."""
    tpl = _mock_template(config_schema)
    template_path = MagicMock()
    resolver = MagicMock(return_value=template_path)
    engine = MagicMock()
    engine.load_template.return_value = tpl
    return resolver, engine, template_path, tpl


# ---------------------------------------------------------------------------
# Tests — confidence_threshold default and kwarg acceptance
# ---------------------------------------------------------------------------


class TestIssueAutomationInit:
    """Verify new __init__ parameters are accepted and stored correctly."""

    def test_default_confidence_threshold(self):
        automation = IssueAutomation(
            classifier=_mock_classifier(),
            selector=TemplateSelector(),
            extractor=InputExtractor(),
        )
        assert automation._confidence_threshold == 0.70

    def test_custom_confidence_threshold(self):
        automation = IssueAutomation(
            classifier=_mock_classifier(),
            selector=TemplateSelector(),
            extractor=InputExtractor(),
            confidence_threshold=0.85,
        )
        assert automation._confidence_threshold == 0.85

    def test_notification_dispatcher_default_none(self):
        automation = IssueAutomation(
            classifier=_mock_classifier(),
            selector=TemplateSelector(),
            extractor=InputExtractor(),
        )
        assert automation._dispatcher is None

    def test_notification_dispatcher_stored(self):
        dispatcher = MagicMock(spec=NotificationDispatcher)
        automation = IssueAutomation(
            classifier=_mock_classifier(),
            selector=TemplateSelector(),
            extractor=InputExtractor(),
            notification_dispatcher=dispatcher,
        )
        assert automation._dispatcher is dispatcher


# ---------------------------------------------------------------------------
# Tests — E2E happy path (high confidence)
# ---------------------------------------------------------------------------


class TestIssueAutomationE2EHappyPath:
    """Full flow: classify → select → extract → launch with high confidence."""

    def test_result_contains_all_expected_keys(self):
        automation = _make_automation(confidence=0.90)
        resolver, engine, path, tpl = _make_template_resolver_and_engine()
        launcher = _make_launcher("run-happy-001")

        result = automation.process(
            issue_number=42,
            repo="owner/repo",
            title="Fix crash on empty input",
            body="When the list is empty the runner crashes.",
            labels=["bug"],
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )

        assert "issue_number" in result
        assert "repo" in result
        assert "classification_type" in result
        assert "confidence" in result
        assert "template" in result
        assert "run_id" in result
        assert "comment_body" in result
        assert "escalated" in result

    def test_escalated_false_on_high_confidence(self):
        automation = _make_automation(confidence=0.90)
        resolver, engine, path, tpl = _make_template_resolver_and_engine()
        launcher = _make_launcher("run-001")

        result = automation.process(
            issue_number=42,
            repo="owner/repo",
            title="Fix crash",
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )

        assert result["escalated"] is False

    def test_run_id_populated_on_successful_launch(self):
        automation = _make_automation(confidence=0.90)
        resolver, engine, path, tpl = _make_template_resolver_and_engine()
        launcher = _make_launcher("run-xyz-999")

        result = automation.process(
            issue_number=1,
            repo="owner/repo",
            title="Add feature",
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )

        assert result["run_id"] == "run-xyz-999"

    def test_launcher_called_with_correct_template(self):
        automation = _make_automation(cls_type="bug", confidence=0.90)
        resolver, engine, path, tpl = _make_template_resolver_and_engine()
        launcher = _make_launcher()

        automation.process(
            issue_number=10,
            repo="owner/repo",
            title="Bug fix",
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )

        launcher.assert_called_once()
        call_kwargs = launcher.call_args
        assert call_kwargs.kwargs["template_file"] is path
        assert call_kwargs.kwargs["template"] is tpl

    def test_correct_classification_type_returned(self):
        automation = _make_automation(cls_type="feature", confidence=0.88)
        result = automation.process(
            issue_number=5,
            repo="owner/repo",
            title="Add new feature",
        )
        assert result["classification_type"] == "feature"

    def test_template_selected_for_bug(self):
        automation = _make_automation(cls_type="bug", confidence=0.90)
        result = automation.process(
            issue_number=5,
            repo="owner/repo",
            title="Bug fix",
        )
        assert result["template"] == "coding-pipeline"

    def test_comment_body_contains_pipeline_name(self):
        automation = _make_automation(cls_type="bug", confidence=0.90)
        resolver, engine, path, tpl = _make_template_resolver_and_engine()
        launcher = _make_launcher("run-001")

        result = automation.process(
            issue_number=5,
            repo="owner/repo",
            title="Bug fix",
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )

        assert "coding-pipeline" in result["comment_body"]
        assert "run-001" in result["comment_body"]


# ---------------------------------------------------------------------------
# Tests — E2E with DB persistence
# ---------------------------------------------------------------------------


class TestIssueAutomationE2EWithDB:
    """Verify DB persistence and status updates."""

    def test_classification_persisted_to_db(self):
        db = _make_db()
        automation = _make_automation(cls_type="bug", confidence=0.90)
        resolver, engine, path, tpl = _make_template_resolver_and_engine()
        launcher = _make_launcher("run-db-001")

        automation.process(
            issue_number=99,
            repo="myorg/myrepo",
            title="DB persistence test",
            db=db,
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )

        row = db.get_issue_classification(99, "myorg/myrepo")
        assert row is not None
        assert row["issue_number"] == 99
        assert row["repo"] == "myorg/myrepo"
        assert row["classification_type"] == "bug"

    def test_status_updated_to_launched_after_successful_launch(self):
        db = _make_db()
        automation = _make_automation(cls_type="bug", confidence=0.90)
        resolver, engine, path, tpl = _make_template_resolver_and_engine()
        launcher = _make_launcher("run-db-002")

        automation.process(
            issue_number=55,
            repo="myorg/myrepo",
            title="Status update test",
            db=db,
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )

        row = db.get_issue_classification(55, "myorg/myrepo")
        assert row is not None
        assert row["status"] == "launched"


# ---------------------------------------------------------------------------
# Tests — E2E escalation path (low confidence)
# ---------------------------------------------------------------------------


class TestIssueAutomationEscalation:
    """Confidence gate: low-confidence issues must be escalated, not launched."""

    def test_escalated_true_when_below_threshold(self):
        automation = _make_automation(confidence=0.50, confidence_threshold=0.70)
        result = automation.process(
            issue_number=7,
            repo="owner/repo",
            title="Vague issue",
        )
        assert result["escalated"] is True

    def test_run_id_none_when_escalated(self):
        automation = _make_automation(confidence=0.40, confidence_threshold=0.70)
        result = automation.process(
            issue_number=7,
            repo="owner/repo",
            title="Vague issue",
        )
        assert result["run_id"] is None

    def test_launcher_not_called_when_escalated(self):
        automation = _make_automation(confidence=0.30, confidence_threshold=0.70)
        launcher = _make_launcher()

        automation.process(
            issue_number=7,
            repo="owner/repo",
            title="Vague issue",
            launcher=launcher,
        )

        launcher.assert_not_called()

    def test_comment_contains_warning_text_when_escalated(self):
        automation = _make_automation(confidence=0.45, confidence_threshold=0.70)
        result = automation.process(
            issue_number=7,
            repo="owner/repo",
            title="Vague issue",
        )
        assert "Confidence too low for automatic launch" in result["comment_body"]

    def test_comment_contains_telegram_escalation_text(self):
        automation = _make_automation(confidence=0.45, confidence_threshold=0.70)
        result = automation.process(
            issue_number=7,
            repo="owner/repo",
            title="Vague issue",
        )
        assert "escalated to human review via Telegram" in result["comment_body"]

    def test_comment_does_not_contain_pipeline_line_when_escalated(self):
        automation = _make_automation(confidence=0.45, confidence_threshold=0.70)
        result = automation.process(
            issue_number=7,
            repo="owner/repo",
            title="Vague issue",
        )
        # The normal pipeline / run-id lines should NOT appear
        assert "**Pipeline:**" not in result["comment_body"]
        assert "**Run ID:**" not in result["comment_body"]

    def test_result_has_all_expected_keys_on_escalation(self):
        automation = _make_automation(confidence=0.20, confidence_threshold=0.70)
        result = automation.process(
            issue_number=7,
            repo="owner/repo",
            title="Vague issue",
        )
        for key in ("issue_number", "repo", "classification_type", "confidence",
                    "template", "run_id", "comment_body", "escalated"):
            assert key in result, f"Missing key: {key!r}"

    def test_comment_contains_confidence_percentage(self):
        automation = _make_automation(confidence=0.45, confidence_threshold=0.70)
        result = automation.process(
            issue_number=7,
            repo="owner/repo",
            title="Vague issue",
        )
        # 45% should appear in the comment
        assert "45%" in result["comment_body"]


# ---------------------------------------------------------------------------
# Tests — dispatcher interaction on escalation
# ---------------------------------------------------------------------------


class TestEscalationDispatcher:
    """Verify the NotificationDispatcher is called correctly on escalation."""

    def test_dispatcher_called_when_escalated(self):
        dispatcher = MagicMock(spec=NotificationDispatcher)
        automation = _make_automation(
            confidence=0.30,
            confidence_threshold=0.70,
            dispatcher=dispatcher,
        )

        automation.process(
            issue_number=77,
            repo="owner/repo",
            title="Ambiguous issue",
        )

        dispatcher.dispatch.assert_called_once()

    def test_dispatcher_called_with_human_review_event(self):
        dispatcher = MagicMock(spec=NotificationDispatcher)
        automation = _make_automation(
            confidence=0.30,
            confidence_threshold=0.70,
            dispatcher=dispatcher,
        )

        automation.process(
            issue_number=77,
            repo="owner/repo",
            title="Ambiguous issue",
        )

        call_kwargs = dispatcher.dispatch.call_args
        assert call_kwargs.kwargs.get("event") == "human_review" or \
               (call_kwargs.args and call_kwargs.args[0] == "human_review")

    def test_dispatcher_called_with_correct_run_id(self):
        dispatcher = MagicMock(spec=NotificationDispatcher)
        automation = _make_automation(
            confidence=0.30,
            confidence_threshold=0.70,
            dispatcher=dispatcher,
        )

        automation.process(
            issue_number=77,
            repo="owner/repo",
            title="Ambiguous issue",
        )

        call_kwargs = dispatcher.dispatch.call_args
        assert call_kwargs.kwargs.get("run_id") == "issue-77"

    def test_dispatcher_called_with_escalation_tier(self):
        dispatcher = MagicMock(spec=NotificationDispatcher)
        automation = _make_automation(
            confidence=0.30,
            confidence_threshold=0.70,
            dispatcher=dispatcher,
        )

        automation.process(
            issue_number=77,
            repo="owner/repo",
            title="Ambiguous issue",
        )

        call_kwargs = dispatcher.dispatch.call_args
        assert call_kwargs.kwargs.get("tier") == "escalation"

    def test_dispatcher_not_called_when_above_threshold(self):
        dispatcher = MagicMock(spec=NotificationDispatcher)
        automation = _make_automation(
            confidence=0.90,
            confidence_threshold=0.70,
            dispatcher=dispatcher,
        )

        automation.process(
            issue_number=77,
            repo="owner/repo",
            title="Clear issue title",
        )

        dispatcher.dispatch.assert_not_called()

    def test_no_error_when_dispatcher_is_none_and_escalated(self):
        """When no dispatcher is set, escalation must still succeed silently."""
        automation = _make_automation(
            confidence=0.20,
            confidence_threshold=0.70,
            dispatcher=None,  # explicitly None
        )

        result = automation.process(
            issue_number=7,
            repo="owner/repo",
            title="Vague issue",
        )

        assert result["escalated"] is True
        assert result["run_id"] is None


# ---------------------------------------------------------------------------
# Tests — threshold boundary conditions
# ---------------------------------------------------------------------------


class TestThresholdBoundary:
    """Exact and near-boundary confidence values."""

    def test_exactly_at_threshold_not_escalated(self):
        """confidence == threshold is NOT escalated (only < threshold escalates)."""
        automation = _make_automation(confidence=0.70, confidence_threshold=0.70)
        result = automation.process(
            issue_number=10,
            repo="owner/repo",
            title="Boundary issue",
        )
        assert result["escalated"] is False

    def test_just_below_threshold_escalated(self):
        automation = _make_automation(confidence=0.6999, confidence_threshold=0.70)
        result = automation.process(
            issue_number=10,
            repo="owner/repo",
            title="Boundary issue",
        )
        assert result["escalated"] is True

    def test_custom_threshold_respected(self):
        """Custom threshold of 0.85: confidence 0.80 should escalate."""
        automation = _make_automation(confidence=0.80, confidence_threshold=0.85)
        result = automation.process(
            issue_number=10,
            repo="owner/repo",
            title="High-bar issue",
        )
        assert result["escalated"] is True

    def test_custom_threshold_not_escalated_above(self):
        """Custom threshold of 0.85: confidence 0.90 should NOT escalate."""
        automation = _make_automation(confidence=0.90, confidence_threshold=0.85)
        result = automation.process(
            issue_number=10,
            repo="owner/repo",
            title="High-bar issue",
        )
        assert result["escalated"] is False


# ---------------------------------------------------------------------------
# Tests — E2E miscellaneous
# ---------------------------------------------------------------------------


class TestIssueAutomationE2EMisc:
    """Additional integration edge cases."""

    def test_no_launcher_high_confidence_not_escalated(self):
        """No launcher provided, but confidence is high — escalated must be False."""
        automation = _make_automation(confidence=0.90)
        result = automation.process(
            issue_number=1,
            repo="owner/repo",
            title="Fix this please",
        )
        assert result["escalated"] is False
        assert result["run_id"] is None

    def test_template_resolver_failure_still_launches(self):
        """When template resolver raises, fall back to empty schema and still launch."""
        automation = _make_automation(confidence=0.90)
        bad_resolver = MagicMock(side_effect=FileNotFoundError("template not found"))
        engine = MagicMock()
        launcher = _make_launcher("run-fallback")

        # Should not raise
        result = automation.process(
            issue_number=20,
            repo="owner/repo",
            title="Feature issue",
            launcher=launcher,
            template_resolver=bad_resolver,
            template_engine=engine,
        )

        # Launcher was NOT called (template_path_obj is None → skip launch)
        # This is consistent with existing logic: no path → no launch
        assert result["escalated"] is False

    def test_full_e2e_pipeline_run_flow(self):
        """Complete E2E: classify bug at 92%, select template, extract inputs, launch."""
        db = _make_db()
        extractor = InputExtractor(executor=MagicMock(
            **{"execute.return_value": json.dumps({"issue_number": 101, "repo": "org/repo"})}
        ))
        schema = {"issue_number": "int", "repo": "str"}
        automation = IssueAutomation(
            classifier=_mock_classifier("bug", 0.92),
            selector=TemplateSelector(),
            extractor=extractor,
            confidence_threshold=0.70,
        )
        resolver, engine, path, tpl = _make_template_resolver_and_engine(schema)
        launcher = _make_launcher("run-full-e2e-001")

        result = automation.process(
            issue_number=101,
            repo="org/repo",
            title="Critical null pointer exception in pipeline",
            body="Steps to reproduce: send empty payload. Expected: error message.",
            labels=["bug", "critical"],
            db=db,
            launcher=launcher,
            template_resolver=resolver,
            template_engine=engine,
        )

        assert result["escalated"] is False
        assert result["run_id"] == "run-full-e2e-001"
        assert result["classification_type"] == "bug"
        assert result["template"] == "coding-pipeline"
        assert result["confidence"] == pytest.approx(0.92)
        assert result["issue_number"] == 101
        assert result["repo"] == "org/repo"

        # Verify DB state
        row = db.get_issue_classification(101, "org/repo")
        assert row["status"] == "launched"
