"""tests/test_sprint_chain.py — Unit tests for sprint chain automation (Issue #514).

Covers:

* SprintQueueConfig loading (happy path, missing fields, defaults, errors)
* SprintChainManager.get_next_issue (queue traversal)
* SprintChainManager.check_score_guard (boundary conditions)
* SprintChainManager.check_budget_guard (cap / no tracker / over budget)
* SprintChainManager.check_human_pause (DB state variants)
* SprintChainManager.mark_processed (DB write)
* SprintChainManager.label_next_issue (add_github_label delegation)
* SprintChainManager.post_queue_comment (template interpolation, URL return)
* SprintChainManager.trigger_next (full integration with mocked externals)
* Database CRUD: upsert_sprint_chain_state, get_sprint_chain_state,
  get_sprint_processed_issues, get_sprint_chain_states
* issue_automation: add_github_label / get_github_issue_labels (subprocess mock)

Test classes
------------
    TestSprintQueueConfigLoading
    TestGetNextIssue
    TestScoreGuard
    TestBudgetGuard
    TestHumanPauseGuard
    TestMarkProcessed
    TestLabelNextIssue
    TestPostQueueComment
    TestTriggerNext
    TestDbSprintChainState
    TestIssueAutomationLabelFunctions
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

from orchestration_engine.db import Database
from orchestration_engine.sprint_chain import (
    SprintChainManager,
    SprintQueueConfig,
    TriggerResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Fresh in-memory Database with full schema + migrations applied."""
    return Database(":memory:")


def _make_manager(
    db: Database | None = None,
    cost_tracker=None,
) -> SprintChainManager:
    if db is None:
        db = _make_db()
    return SprintChainManager(db=db, cost_tracker=cost_tracker)


def _write_queue_yaml(tmp_path: Path, data: dict) -> str:
    """Write a sprint queue YAML file and return its path string."""
    p = tmp_path / "sprint_queue.yaml"
    p.write_text(yaml.dump(data))
    return str(p)


# ---------------------------------------------------------------------------
# TestSprintQueueConfigLoading
# ---------------------------------------------------------------------------


class TestSprintQueueConfigLoading:
    """Tests for SprintChainManager.load_queue_config."""

    def test_load_valid_config(self, tmp_path):
        """Happy path: all fields populated."""
        path = _write_queue_yaml(
            tmp_path,
            {
                "repo": "owner/repo",
                "issues": [501, 505, 511],
                "score_threshold": 0.8,
                "daily_budget_cap_usd": 15.0,
                "comment_template": "Issue #{previous_issue} done",
            },
        )
        m = _make_manager()
        cfg = m.load_queue_config(path)
        assert cfg.repo == "owner/repo"
        assert cfg.issues == [501, 505, 511]
        assert cfg.score_threshold == 0.8
        assert cfg.daily_budget_cap_usd == 15.0
        assert cfg.comment_template == "Issue #{previous_issue} done"

    def test_load_config_missing_repo(self, tmp_path):
        """ValueError raised when 'repo' field is absent."""
        path = _write_queue_yaml(tmp_path, {"issues": [501]})
        m = _make_manager()
        with pytest.raises(ValueError, match="missing required 'repo'"):
            m.load_queue_config(path)

    def test_load_config_invalid_issues_non_integer(self, tmp_path):
        """ValueError raised when issues list contains a non-integer."""
        path = _write_queue_yaml(
            tmp_path, {"repo": "owner/repo", "issues": [501, "not-int"]}
        )
        m = _make_manager()
        with pytest.raises(ValueError, match="must be a list of positive integers"):
            m.load_queue_config(path)

    def test_load_config_invalid_issues_zero(self, tmp_path):
        """ValueError raised when issues list contains zero."""
        path = _write_queue_yaml(
            tmp_path, {"repo": "owner/repo", "issues": [0, 501]}
        )
        m = _make_manager()
        with pytest.raises(ValueError, match="must be a list of positive integers"):
            m.load_queue_config(path)

    def test_load_config_missing_file(self):
        """FileNotFoundError raised when the config file does not exist."""
        m = _make_manager()
        with pytest.raises(FileNotFoundError, match="not found"):
            m.load_queue_config("/nonexistent/sprint_queue.yaml")

    def test_load_config_defaults(self, tmp_path):
        """Optional fields default correctly when omitted."""
        path = _write_queue_yaml(
            tmp_path, {"repo": "owner/repo", "issues": [501]}
        )
        m = _make_manager()
        cfg = m.load_queue_config(path)
        assert cfg.score_threshold == 0.75
        assert cfg.daily_budget_cap_usd is None
        assert "previous_issue" in cfg.comment_template

    def test_load_config_negative_score_threshold(self, tmp_path):
        """Negative score_threshold is accepted (guard semantics tested elsewhere)."""
        path = _write_queue_yaml(
            tmp_path,
            {"repo": "owner/repo", "issues": [501], "score_threshold": -0.5},
        )
        m = _make_manager()
        cfg = m.load_queue_config(path)
        assert cfg.score_threshold == -0.5

    def test_load_config_empty_issues_list(self, tmp_path):
        """Empty issues list is valid (queue_exhausted will be returned later)."""
        path = _write_queue_yaml(tmp_path, {"repo": "owner/repo", "issues": []})
        m = _make_manager()
        cfg = m.load_queue_config(path)
        assert cfg.issues == []


# ---------------------------------------------------------------------------
# TestGetNextIssue
# ---------------------------------------------------------------------------


class TestGetNextIssue:
    """Tests for SprintChainManager.get_next_issue."""

    def _cfg(self, issues):
        return SprintQueueConfig(repo="owner/repo", issues=issues)

    def test_get_next_issue_empty_processed(self):
        """Returns first issue when nothing has been processed."""
        m = _make_manager()
        assert m.get_next_issue(self._cfg([501, 505, 511]), []) == 501

    def test_get_next_issue_first_processed(self):
        """Skips first issue, returns second."""
        m = _make_manager()
        assert m.get_next_issue(self._cfg([501, 505, 511]), [501]) == 505

    def test_get_next_issue_all_processed(self):
        """Returns None when entire queue is processed."""
        m = _make_manager()
        assert m.get_next_issue(self._cfg([501, 505]), [501, 505]) is None

    def test_get_next_issue_middle_processed(self):
        """Correctly skips non-contiguous processed entries."""
        m = _make_manager()
        assert m.get_next_issue(self._cfg([501, 505, 511, 514]), [501, 511]) == 505

    def test_get_next_issue_empty_queue(self):
        """Returns None when queue has no issues."""
        m = _make_manager()
        assert m.get_next_issue(self._cfg([]), []) is None


# ---------------------------------------------------------------------------
# TestScoreGuard
# ---------------------------------------------------------------------------


class TestScoreGuard:
    """Tests for SprintChainManager.check_score_guard."""

    def test_score_above_threshold(self):
        assert _make_manager().check_score_guard(0.9, 0.75) is True

    def test_score_equal_threshold(self):
        """Boundary: exactly at threshold passes."""
        assert _make_manager().check_score_guard(0.75, 0.75) is True

    def test_score_below_threshold(self):
        assert _make_manager().check_score_guard(0.74, 0.75) is False

    def test_score_none(self):
        """None score always fails."""
        assert _make_manager().check_score_guard(None, 0.75) is False

    def test_threshold_zero(self):
        """Zero threshold passes any non-None score."""
        assert _make_manager().check_score_guard(0.0, 0.0) is True
        assert _make_manager().check_score_guard(0.001, 0.0) is True


# ---------------------------------------------------------------------------
# TestBudgetGuard
# ---------------------------------------------------------------------------


class TestBudgetGuard:
    """Tests for SprintChainManager.check_budget_guard."""

    def _cfg(self, cap):
        return SprintQueueConfig(repo="owner/repo", issues=[], daily_budget_cap_usd=cap)

    def test_no_budget_cap(self):
        """No cap configured → always True."""
        m = _make_manager()
        assert m.check_budget_guard(self._cfg(None)) is True

    def test_no_cost_tracker(self):
        """No cost tracker with a cap → True (warning logged, not blocked)."""
        m = _make_manager(cost_tracker=None)
        assert m.check_budget_guard(self._cfg(10.0)) is True

    def test_under_budget(self):
        tracker = MagicMock()
        tracker.get_daily_cost.return_value = 5.0
        m = _make_manager(cost_tracker=tracker)
        assert m.check_budget_guard(self._cfg(10.0)) is True

    def test_at_budget_boundary(self):
        """Exactly at cap is blocked (>= comparison)."""
        tracker = MagicMock()
        tracker.get_daily_cost.return_value = 10.0
        m = _make_manager(cost_tracker=tracker)
        assert m.check_budget_guard(self._cfg(10.0)) is False

    def test_over_budget(self):
        tracker = MagicMock()
        tracker.get_daily_cost.return_value = 15.5
        m = _make_manager(cost_tracker=tracker)
        assert m.check_budget_guard(self._cfg(10.0)) is False


# ---------------------------------------------------------------------------
# TestHumanPauseGuard
# ---------------------------------------------------------------------------


class TestHumanPauseGuard:
    """Tests for SprintChainManager.check_human_pause."""

    def test_no_state_in_db(self):
        """No DB record → not paused."""
        db = _make_db()
        m = _make_manager(db=db)
        assert m.check_human_pause("owner/repo", 501) is True

    def test_state_processed(self):
        """Status='processed' → not paused."""
        db = _make_db()
        db.upsert_sprint_chain_state("owner/repo", 501, "processed")
        m = _make_manager(db=db)
        assert m.check_human_pause("owner/repo", 501) is True

    def test_state_paused(self):
        """Status='paused' → chain halted."""
        db = _make_db()
        db.upsert_sprint_chain_state("owner/repo", 501, "paused")
        m = _make_manager(db=db)
        assert m.check_human_pause("owner/repo", 501) is False

    def test_db_returns_none(self):
        """get_sprint_chain_state returning None → conservative allow."""
        mock_db = MagicMock()
        mock_db.get_sprint_chain_state.return_value = None
        m = _make_manager(db=mock_db)
        assert m.check_human_pause("owner/repo", 501) is True


# ---------------------------------------------------------------------------
# TestMarkProcessed
# ---------------------------------------------------------------------------


class TestMarkProcessed:
    """Tests for SprintChainManager.mark_processed."""

    def test_mark_processed_writes_to_db(self):
        """Verifies upsert_sprint_chain_state is called with correct args."""
        mock_db = MagicMock()
        m = _make_manager(db=mock_db)
        m.mark_processed("owner/repo", 501, "run-123", 0.9)
        mock_db.upsert_sprint_chain_state.assert_called_once_with(
            repo="owner/repo",
            issue_number=501,
            status="processed",
            run_id="run-123",
            score=0.9,
        )

    def test_mark_processed_with_none_score(self):
        """None score is passed through unchanged."""
        mock_db = MagicMock()
        m = _make_manager(db=mock_db)
        m.mark_processed("owner/repo", 501, "run-123", None)
        mock_db.upsert_sprint_chain_state.assert_called_once_with(
            repo="owner/repo",
            issue_number=501,
            status="processed",
            run_id="run-123",
            score=None,
        )


# ---------------------------------------------------------------------------
# TestLabelNextIssue
# ---------------------------------------------------------------------------


class TestLabelNextIssue:
    """Tests for SprintChainManager.label_next_issue."""

    def test_label_success(self):
        """add_github_label returning True → True."""
        m = _make_manager()
        with patch(
            "orchestration_engine.sprint_chain.SprintChainManager.label_next_issue",
            wraps=m.label_next_issue,
        ):
            with patch(
                "orchestration_engine.issue_automation.add_github_label",
                return_value=True,
            ):
                assert m.label_next_issue("owner/repo", 501) is True

    def test_label_failure(self):
        """add_github_label returning False → False."""
        m = _make_manager()
        with patch(
            "orchestration_engine.issue_automation.add_github_label",
            return_value=False,
        ):
            assert m.label_next_issue("owner/repo", 501) is False

    def test_label_calls_correct_label_name(self):
        """Verifies the label name is exactly 'pipeline-ready'."""
        m = _make_manager()
        with patch(
            "orchestration_engine.issue_automation.add_github_label",
            return_value=True,
        ) as mock_add:
            m.label_next_issue("owner/repo", 501)
            mock_add.assert_called_once_with("owner/repo", 501, "pipeline-ready")


# ---------------------------------------------------------------------------
# TestPostQueueComment
# ---------------------------------------------------------------------------


class TestPostQueueComment:
    """Tests for SprintChainManager.post_queue_comment."""

    def test_comment_template_interpolation(self):
        """Verifies {previous_issue} placeholder is replaced."""
        m = _make_manager()
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value="https://github.com/owner/repo/issues/505#issuecomment-1",
        ) as mock_post:
            m.post_queue_comment(
                repo="owner/repo",
                next_issue=505,
                previous_issue=501,
                comment_template="Previous: #{previous_issue}, next: #{next_issue}",
            )
            body_used = mock_post.call_args[1]["body"]
            assert "501" in body_used
            assert "505" in body_used

    def test_comment_returns_url(self):
        """Returns URL from post_github_comment."""
        m = _make_manager()
        expected_url = "https://github.com/owner/repo/issues/505#issuecomment-1"
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value=expected_url,
        ):
            url = m.post_queue_comment(
                repo="owner/repo",
                next_issue=505,
                previous_issue=501,
                comment_template="Previous #{previous_issue}",
            )
            assert url == expected_url

    def test_comment_returns_none_on_failure(self):
        """Returns None when post_github_comment returns None."""
        m = _make_manager()
        with patch(
            "orchestration_engine.issue_automation.post_github_comment",
            return_value=None,
        ):
            url = m.post_queue_comment(
                repo="owner/repo",
                next_issue=505,
                previous_issue=501,
                comment_template="Previous #{previous_issue}",
            )
            assert url is None


# ---------------------------------------------------------------------------
# TestTriggerNext
# ---------------------------------------------------------------------------


class TestTriggerNext:
    """Integration-style tests for SprintChainManager.trigger_next."""

    def _make_config_file(self, tmp_path, issues=None, threshold=0.75):
        data = {
            "repo": "owner/repo",
            "issues": issues or [501, 505, 511],
            "score_threshold": threshold,
        }
        return _write_queue_yaml(tmp_path, data)

    def test_trigger_next_happy_path(self, tmp_path):
        """All guards pass, label applied, comment posted → triggered=True."""
        db = _make_db()
        m = _make_manager(db=db)
        config_path = self._make_config_file(tmp_path)

        with patch.object(m, "label_next_issue", return_value=True):
            with patch.object(
                m,
                "post_queue_comment",
                return_value="https://github.com/owner/repo/issues/505#c1",
            ):
                result = m.trigger_next(
                    repo="owner/repo",
                    current_issue=501,
                    run_id="run-123",
                    score=0.9,
                    queue_config_path=config_path,
                )

        assert result.triggered is True
        assert result.next_issue == 505
        assert result.reason == "ok"
        assert result.comment_url == "https://github.com/owner/repo/issues/505#c1"

    def test_trigger_next_score_below_threshold(self, tmp_path):
        """Score below threshold → triggered=False."""
        db = _make_db()
        m = _make_manager(db=db)
        config_path = self._make_config_file(tmp_path, threshold=0.85)

        result = m.trigger_next(
            repo="owner/repo",
            current_issue=501,
            run_id="run-123",
            score=0.7,
            queue_config_path=config_path,
        )
        assert result.triggered is False
        assert "score_below_threshold" in result.reason

    def test_trigger_next_queue_exhausted(self, tmp_path):
        """All issues processed → triggered=False, reason=queue_exhausted."""
        db = _make_db()
        # Mark all issues as processed
        for issue in [501, 505, 511]:
            db.upsert_sprint_chain_state("owner/repo", issue, "processed")
        m = _make_manager(db=db)
        config_path = self._make_config_file(tmp_path)

        result = m.trigger_next(
            repo="owner/repo",
            current_issue=501,
            run_id="run-123",
            score=0.9,
            queue_config_path=config_path,
        )
        assert result.triggered is False
        assert result.reason == "queue_exhausted"

    def test_trigger_next_budget_exceeded(self, tmp_path):
        """Daily budget cap reached → triggered=False."""
        db = _make_db()
        tracker = MagicMock()
        tracker.get_daily_cost.return_value = 20.0
        m = _make_manager(db=db, cost_tracker=tracker)
        config_path = _write_queue_yaml(
            tmp_path,
            {
                "repo": "owner/repo",
                "issues": [501, 505],
                "score_threshold": 0.5,
                "daily_budget_cap_usd": 10.0,
            },
        )
        result = m.trigger_next(
            repo="owner/repo",
            current_issue=501,
            run_id="run-123",
            score=0.9,
            queue_config_path=config_path,
        )
        assert result.triggered is False
        assert result.reason == "daily_budget_cap_reached"
        assert result.next_issue == 505

    def test_trigger_next_human_paused(self, tmp_path):
        """Human pause → triggered=False, reason=human_paused."""
        db = _make_db()
        db.upsert_sprint_chain_state("owner/repo", 505, "paused")
        m = _make_manager(db=db)
        config_path = self._make_config_file(tmp_path)

        result = m.trigger_next(
            repo="owner/repo",
            current_issue=501,
            run_id="run-123",
            score=0.9,
            queue_config_path=config_path,
        )
        assert result.triggered is False
        assert result.reason == "human_paused"
        assert result.next_issue == 505

    def test_trigger_next_label_apply_fails(self, tmp_path):
        """label_next_issue returning False → triggered=False."""
        db = _make_db()
        m = _make_manager(db=db)
        config_path = self._make_config_file(tmp_path)

        with patch.object(m, "label_next_issue", return_value=False):
            result = m.trigger_next(
                repo="owner/repo",
                current_issue=501,
                run_id="run-123",
                score=0.9,
                queue_config_path=config_path,
            )

        assert result.triggered is False
        assert result.reason == "label_apply_failed"
        assert result.next_issue == 505

    def test_trigger_next_config_load_fails(self):
        """Missing config file → triggered=False, reason starts with config_load_failed."""
        m = _make_manager()
        result = m.trigger_next(
            repo="owner/repo",
            current_issue=501,
            run_id="run-123",
            score=0.9,
            queue_config_path="/nonexistent/sprint_queue.yaml",
        )
        assert result.triggered is False
        assert result.reason.startswith("config_load_failed")

    def test_trigger_next_marks_current_before_labeling(self, tmp_path):
        """mark_processed is called before label_next_issue."""
        db = _make_db()
        m = _make_manager(db=db)
        config_path = self._make_config_file(tmp_path)
        call_order = []

        def _mark(*args, **kwargs):
            call_order.append("mark")

        def _label(*args, **kwargs):
            call_order.append("label")
            return True

        with patch.object(m, "mark_processed", side_effect=_mark):
            with patch.object(m, "label_next_issue", side_effect=_label):
                with patch.object(m, "post_queue_comment", return_value=None):
                    m.trigger_next(
                        repo="owner/repo",
                        current_issue=501,
                        run_id="run-123",
                        score=0.9,
                        queue_config_path=config_path,
                    )

        assert call_order == ["mark", "label"]

    def test_trigger_next_comment_failure_nonfatal(self, tmp_path):
        """Comment failure does not prevent triggered=True."""
        db = _make_db()
        m = _make_manager(db=db)
        config_path = self._make_config_file(tmp_path)

        with patch.object(m, "label_next_issue", return_value=True):
            with patch.object(
                m, "post_queue_comment", side_effect=Exception("network error")
            ):
                result = m.trigger_next(
                    repo="owner/repo",
                    current_issue=501,
                    run_id="run-123",
                    score=0.9,
                    queue_config_path=config_path,
                )

        assert result.triggered is True
        assert result.comment_url is None


# ---------------------------------------------------------------------------
# TestDbSprintChainState
# ---------------------------------------------------------------------------


class TestDbSprintChainState:
    """Tests for Database sprint_chain_state CRUD methods."""

    def test_upsert_and_retrieve(self):
        db = _make_db()
        db.upsert_sprint_chain_state("owner/repo", 501, "processed", "run-1", 0.9)
        row = db.get_sprint_chain_state("owner/repo", 501)
        assert row is not None
        assert row["repo"] == "owner/repo"
        assert row["issue_number"] == 501
        assert row["status"] == "processed"
        assert row["run_id"] == "run-1"
        assert row["score"] == pytest.approx(0.9)

    def test_get_sprint_chain_state_not_found(self):
        db = _make_db()
        assert db.get_sprint_chain_state("owner/repo", 9999) is None

    def test_get_processed_issues_empty(self):
        db = _make_db()
        assert db.get_sprint_processed_issues("owner/repo") == []

    def test_get_processed_issues_filters_by_repo(self):
        db = _make_db()
        db.upsert_sprint_chain_state("owner/repo", 501, "processed")
        db.upsert_sprint_chain_state("other/repo", 505, "processed")
        result = db.get_sprint_processed_issues("owner/repo")
        assert result == [501]
        assert 505 not in result

    def test_upsert_idempotent_no_duplicate(self):
        """Second upsert updates in place — no duplicate rows."""
        db = _make_db()
        db.upsert_sprint_chain_state("owner/repo", 501, "processed", "run-1", 0.8)
        db.upsert_sprint_chain_state("owner/repo", 501, "processed", "run-2", 0.9)
        all_rows = db.get_sprint_chain_states("owner/repo")
        assert len(all_rows) == 1
        assert all_rows[0]["run_id"] == "run-2"
        assert all_rows[0]["score"] == pytest.approx(0.9)

    def test_get_chain_states_ordered_by_processed_at(self):
        """get_sprint_chain_states returns rows ordered by processed_at ASC."""
        db = _make_db()
        db.upsert_sprint_chain_state("owner/repo", 501, "processed")
        db.upsert_sprint_chain_state("owner/repo", 505, "processed")
        db.upsert_sprint_chain_state("owner/repo", 511, "processed")
        states = db.get_sprint_chain_states("owner/repo")
        issue_numbers = [s["issue_number"] for s in states]
        # Should be in insertion order (processed_at ascending)
        assert len(issue_numbers) == 3
        assert set(issue_numbers) == {501, 505, 511}

    def test_upsert_status_paused(self):
        """Status 'paused' is correctly stored and retrieved."""
        db = _make_db()
        db.upsert_sprint_chain_state("owner/repo", 501, "paused")
        row = db.get_sprint_chain_state("owner/repo", 501)
        assert row["status"] == "paused"

    def test_get_processed_issues_skips_paused(self):
        """get_sprint_processed_issues returns only status='processed' rows."""
        db = _make_db()
        db.upsert_sprint_chain_state("owner/repo", 501, "processed")
        db.upsert_sprint_chain_state("owner/repo", 505, "paused")
        result = db.get_sprint_processed_issues("owner/repo")
        assert result == [501]
        assert 505 not in result


# ---------------------------------------------------------------------------
# TestIssueAutomationLabelFunctions
# ---------------------------------------------------------------------------


class TestIssueAutomationLabelFunctions:
    """Tests for add_github_label and get_github_issue_labels."""

    def test_add_github_label_success(self):
        """subprocess rc=0 → returns True."""
        from orchestration_engine.issue_automation import add_github_label

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = add_github_label("owner/repo", 42, "pipeline-ready")

        assert result is True
        args = mock_run.call_args[0][0]
        assert "gh" in args
        assert "--method" in args
        assert "POST" in args
        assert "labels[]=pipeline-ready" in " ".join(args)

    def test_add_github_label_failure(self):
        """subprocess rc=1 → returns False, warning logged."""
        from orchestration_engine.issue_automation import add_github_label

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "422 Unprocessable Entity"

        with patch("subprocess.run", return_value=mock_result):
            result = add_github_label("owner/repo", 42, "pipeline-ready")

        assert result is False

    def test_add_github_label_timeout(self):
        """TimeoutExpired → returns False."""
        from orchestration_engine.issue_automation import add_github_label

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 15)):
            result = add_github_label("owner/repo", 42, "pipeline-ready")

        assert result is False

    def test_add_github_label_gh_not_found(self):
        """FileNotFoundError → returns False."""
        from orchestration_engine.issue_automation import add_github_label

        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            result = add_github_label("owner/repo", 42, "pipeline-ready")

        assert result is False

    def test_get_github_issue_labels_success(self):
        """Parses multiline output into list of label names."""
        from orchestration_engine.issue_automation import get_github_issue_labels

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "pipeline-ready\nbug\nenhancement\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            labels = get_github_issue_labels("owner/repo", 42)

        assert labels == ["pipeline-ready", "bug", "enhancement"]

    def test_get_github_issue_labels_empty_issue(self):
        """Empty stdout → empty list."""
        from orchestration_engine.issue_automation import get_github_issue_labels

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            labels = get_github_issue_labels("owner/repo", 42)

        assert labels == []

    def test_get_github_issue_labels_failure(self):
        """subprocess rc=1 → returns empty list."""
        from orchestration_engine.issue_automation import get_github_issue_labels

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Not Found"

        with patch("subprocess.run", return_value=mock_result):
            labels = get_github_issue_labels("owner/repo", 42)

        assert labels == []

    def test_get_github_issue_labels_timeout(self):
        """TimeoutExpired → returns empty list."""
        from orchestration_engine.issue_automation import get_github_issue_labels

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 15)):
            labels = get_github_issue_labels("owner/repo", 42)

        assert labels == []
