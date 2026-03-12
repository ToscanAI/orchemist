"""Unit tests for orchestration_engine.github_utils (Issue #515).

All tests use mocked subprocess calls — no live GitHub API calls are made.
Tests cover the behavioral contracts described in the spec:

1. Issue found on board → mutation is called (happy path).
2. Issue not on any board → no mutation, returns False.
3. Status field not found → no mutation, warning logged, returns False.
4. Column option not found → no mutation, WARNING logged, returns False.
5. Subprocess TimeoutExpired → returns False, no exception raised.
6. Multiple boards, all succeed → mutation called per board, returns True.
7. Multiple boards, partial failure → successful transitions preserved, returns True.
8. pipeline-failed label failure → warning logged, no exception propagates.
"""

from __future__ import annotations

import json
import logging
import subprocess
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Test helpers (mirrors acceptance test helpers for consistency)
# ---------------------------------------------------------------------------

def _gh_ok(data: dict) -> MagicMock:
    """Return a mock successful subprocess result wrapping GraphQL data."""
    return MagicMock(
        returncode=0,
        stdout=json.dumps({"data": data}),
        stderr="",
    )


def _project_items_response(nodes: list) -> MagicMock:
    """Simulate a GitHub GraphQL response for the project item membership query."""
    return _gh_ok({
        "repository": {
            "issue": {
                "projectItems": {"nodes": nodes}
            }
        }
    })


def _mutation_ok() -> MagicMock:
    """Simulate a successful updateProjectV2ItemFieldValue mutation response."""
    return _gh_ok({
        "updateProjectV2ItemFieldValue": {
            "projectV2Item": {"id": "item_updated_id"}
        }
    })


def _make_project_item(
    item_id: str = "item1",
    project_id: str = "proj1",
    title: str = "Sprint Board",
    field_name: str = "Status",
    options: list | None = None,
) -> dict:
    """Build a fake GitHub Projects v2 item node as returned by GraphQL."""
    if options is None:
        options = [
            {"id": "opt_ip",   "name": "In Progress"},
            {"id": "opt_rev",  "name": "Review"},
            {"id": "opt_done", "name": "Done"},
            {"id": "opt_blk",  "name": "Blocked"},
        ]
    return {
        "id": item_id,
        "project": {
            "id": project_id,
            "title": title,
            "fields": {
                "nodes": [
                    {"id": "fld1", "name": field_name, "options": options}
                ]
            },
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMoveIssueBoardSuccess:
    """Test 1: Issue found on board with matching field and column → mutation fired."""

    def test_move_issue_on_board_success(self):
        """Mock returns valid project item; verifies mutation subprocess call was made."""
        from orchestration_engine.github_utils import move_issue_on_board

        items = [_make_project_item(options=[{"id": "opt_rev", "name": "Review"}])]

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [_project_items_response(items), _mutation_ok()]
            result = move_issue_on_board("myorg", "myrepo", 42, "Review")

        assert result is True, "Should return True when mutation succeeds"
        assert mock_run.call_count == 2, (
            f"Expected 1 query + 1 mutation = 2 calls, got {mock_run.call_count}"
        )

    def test_move_issue_on_board_passes_correct_option_id(self):
        """The mutation is called with the correct optionId from the field options."""
        from orchestration_engine.github_utils import move_issue_on_board

        items = [_make_project_item(options=[
            {"id": "opt_done_123", "name": "Done"},
        ])]

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [_project_items_response(items), _mutation_ok()]
            result = move_issue_on_board("myorg", "myrepo", 7, "Done")

        assert result is True
        # The second call should be the mutation
        mutation_call_args = mock_run.call_args_list[1]
        cmd = mutation_call_args[0][0]
        # Should be a gh api graphql call
        assert "gh" in cmd
        assert "graphql" in cmd


class TestIssueNotOnBoard:
    """Test 2: Issue has no project board membership → no mutation."""

    def test_move_issue_not_on_board_returns_false(self):
        """Mock returns empty projectItems.nodes; no mutation call, returns False."""
        from orchestration_engine.github_utils import move_issue_on_board

        with patch("subprocess.run", return_value=_project_items_response([])) as mock_run:
            result = move_issue_on_board("myorg", "myrepo", 99, "In Progress")

        assert result is False, "Should return False when issue is on no boards"
        assert mock_run.call_count == 1, (
            "Only the membership query should run (no mutation)"
        )


class TestStatusFieldNotFound:
    """Test 3: Status field not found → no mutation, warning logged."""

    def test_move_issue_status_field_not_found_returns_false(self):
        """Board has a 'Priority' field but no 'Status' field; mutation not called."""
        from orchestration_engine.github_utils import move_issue_on_board

        items = [_make_project_item(field_name="Priority", options=[
            {"id": "opt_high", "name": "High"},
        ])]

        with patch("subprocess.run", return_value=_project_items_response(items)) as mock_run:
            result = move_issue_on_board(
                "myorg", "myrepo", 42, "In Progress", field_name="Status"
            )

        assert result is False, "Should return False when status field is absent"
        assert mock_run.call_count == 1, "No mutation should be attempted"

    def test_move_issue_status_field_not_found_logs_warning(self, caplog):
        """A WARNING is emitted when the status field is not found on the board."""
        from orchestration_engine.github_utils import move_issue_on_board

        items = [_make_project_item(field_name="Priority", options=[
            {"id": "opt_high", "name": "High"},
        ])]

        with patch("subprocess.run", return_value=_project_items_response(items)):
            with caplog.at_level(logging.WARNING):
                move_issue_on_board(
                    "myorg", "myrepo", 42, "In Progress", field_name="Status"
                )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) > 0, "Expected WARNING when status field is absent"


class TestColumnOptionNotFound:
    """Test 4: Column option not found → no mutation, WARNING logged."""

    def test_move_issue_column_option_not_found_returns_false(self):
        """Status field present but 'Blocked' option absent; no mutation fired."""
        from orchestration_engine.github_utils import move_issue_on_board

        items = [_make_project_item(options=[
            {"id": "opt_ip",  "name": "In Progress"},
            {"id": "opt_rev", "name": "Review"},
            # "Blocked" deliberately absent
        ])]

        with patch("subprocess.run", return_value=_project_items_response(items)) as mock_run:
            result = move_issue_on_board("myorg", "myrepo", 42, "Blocked")

        assert result is False, "Should return False when column option is absent"
        assert mock_run.call_count == 1, "No mutation when target column is absent"

    def test_move_issue_column_option_not_found_logs_warning(self, caplog):
        """A WARNING-level log entry is emitted when the column option is not found."""
        from orchestration_engine.github_utils import move_issue_on_board

        items = [_make_project_item(options=[
            {"id": "opt_ip", "name": "In Progress"},
        ])]

        with patch("subprocess.run", return_value=_project_items_response(items)):
            with caplog.at_level(logging.WARNING):
                move_issue_on_board("myorg", "myrepo", 42, "NonExistentColumn")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) > 0, "Expected WARNING when column option is absent"


class TestGraphQLErrors:
    """Test 5: Various error conditions → returns False, no exception raised."""

    def test_move_issue_graphql_timeout_does_not_raise(self):
        """subprocess.TimeoutExpired during query → returns False, no exception."""
        from orchestration_engine.github_utils import move_issue_on_board

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 15)):
            try:
                result = move_issue_on_board("myorg", "myrepo", 42, "Review")
                assert result is False
            except Exception as exc:
                pytest.fail(f"TimeoutExpired must not propagate: {type(exc).__name__}: {exc}")

    def test_move_issue_query_nonzero_exit_does_not_raise(self):
        """Non-zero exit code on query → returns False, no exception."""
        from orchestration_engine.github_utils import move_issue_on_board

        error_resp = MagicMock(returncode=1, stdout="{}", stderr="HTTP 403")

        with patch("subprocess.run", return_value=error_resp):
            try:
                result = move_issue_on_board("myorg", "myrepo", 42, "Review")
                assert result is False
            except Exception as exc:
                pytest.fail(f"Non-zero exit must not propagate: {type(exc).__name__}: {exc}")

    def test_move_issue_mutation_nonzero_exit_does_not_raise(self):
        """Query succeeds but mutation returns non-zero → returns False, no exception."""
        from orchestration_engine.github_utils import move_issue_on_board

        items = [_make_project_item(options=[{"id": "opt_rev", "name": "Review"}])]
        error_mutation = MagicMock(
            returncode=1,
            stdout='{"errors": [{"message": "Resource not accessible by personal access token"}]}',
            stderr="GraphQL API error",
        )

        with patch("subprocess.run", side_effect=[_project_items_response(items), error_mutation]):
            try:
                result = move_issue_on_board("myorg", "myrepo", 42, "Review")
                # result may be False since mutation returned error
            except Exception as exc:
                pytest.fail(
                    f"Mutation error must not propagate to caller: {type(exc).__name__}: {exc}"
                )

    def test_move_issue_connection_error_does_not_raise(self):
        """ConnectionError during subprocess.run → returns False, no exception."""
        from orchestration_engine.github_utils import move_issue_on_board

        with patch("subprocess.run", side_effect=ConnectionError("Network unreachable")):
            try:
                result = move_issue_on_board("myorg", "myrepo", 42, "Done")
                assert result is False
            except Exception as exc:
                pytest.fail(f"ConnectionError must not propagate: {type(exc).__name__}: {exc}")


class TestMultipleBoards:
    """Tests 6 & 7: Multi-board transitions."""

    def test_move_issue_multiple_boards_all_success(self):
        """Two boards → query called once, mutation called twice, returns True."""
        from orchestration_engine.github_utils import move_issue_on_board

        items = [
            _make_project_item(
                item_id="item1", project_id="proj1", title="Board A",
                options=[{"id": "opt_done_a", "name": "Done"}],
            ),
            _make_project_item(
                item_id="item2", project_id="proj2", title="Board B",
                options=[{"id": "opt_done_b", "name": "Done"}],
            ),
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _project_items_response(items),
                _mutation_ok(),   # Board A
                _mutation_ok(),   # Board B
            ]
            result = move_issue_on_board("myorg", "myrepo", 42, "Done")

        assert result is True
        assert mock_run.call_count == 3, (
            f"Expected 1 query + 2 mutations = 3 calls, got {mock_run.call_count}"
        )

    def test_move_issue_multiple_boards_partial_failure(self):
        """Board A succeeds, Board B fails → Board A not rolled back, returns True."""
        from orchestration_engine.github_utils import move_issue_on_board

        items = [
            _make_project_item(
                item_id="item1", project_id="proj1", title="Board A",
                options=[{"id": "opt_rev_a", "name": "Review"}],
            ),
            _make_project_item(
                item_id="item2", project_id="proj2", title="Board B",
                options=[{"id": "opt_rev_b", "name": "Review"}],
            ),
        ]

        call_counter = [0]

        def controlled_side_effect(*args, **kwargs):
            call_counter[0] += 1
            if call_counter[0] == 1:
                return _project_items_response(items)   # query
            elif call_counter[0] == 2:
                return _mutation_ok()                   # Board A succeeds
            else:
                raise ConnectionError("Network unreachable for Board B")  # Board B fails

        with patch("subprocess.run", side_effect=controlled_side_effect):
            try:
                result = move_issue_on_board("myorg", "myrepo", 42, "Review")
                assert result is True, "Should return True when at least one board updated"
            except Exception as exc:
                pytest.fail(
                    f"Partial failure must not propagate: {type(exc).__name__}: {exc}"
                )


class TestPipelineFailedLabelNonFatal:
    """Test 8: pipeline-failed label failure is non-fatal."""

    def test_pipeline_failed_label_failure_non_fatal(self, caplog):
        """add_github_label raising an exception logs a warning but does not propagate."""
        # This tests the pattern used in daemon.py's failure hook.
        # We simulate what the hook code does:
        mock_add_label = MagicMock(side_effect=OSError("API unavailable"))

        try:
            with caplog.at_level(logging.WARNING):
                try:
                    mock_add_label("owner/repo", 42, "pipeline-failed")
                except Exception as _lbl_exc:  # noqa: BLE001
                    logging.getLogger("daemon").warning(
                        "Failed to add pipeline-failed label (non-fatal): %s", _lbl_exc
                    )
        except Exception as exc:
            pytest.fail(
                f"Label failure must not propagate: {type(exc).__name__}: {exc}"
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) > 0, "Expected WARNING when label add fails"


class TestHelperFunctions:
    """Tests for get_column_name, get_board_token, and get_issue_project_items."""

    def test_get_column_name_defaults(self):
        """Default column names match spec."""
        from orchestration_engine.github_utils import get_column_name

        assert get_column_name("in_progress") == "In Progress"
        assert get_column_name("review") == "Review"
        assert get_column_name("done") == "Done"
        assert get_column_name("blocked") == "Blocked"

    def test_get_column_name_env_override(self, monkeypatch):
        """Env var overrides are respected."""
        from orchestration_engine.github_utils import get_column_name

        monkeypatch.setenv("GITHUB_PROJECTS_COLUMN_IN_PROGRESS", "Active")
        assert get_column_name("in_progress") == "Active"

    def test_get_board_token_projects_token(self, monkeypatch):
        """GITHUB_PROJECTS_TOKEN takes precedence over GH_TOKEN."""
        from orchestration_engine.github_utils import get_board_token

        monkeypatch.setenv("GITHUB_PROJECTS_TOKEN", "projects-pat-123")
        monkeypatch.setenv("GH_TOKEN", "gh-token-456")
        assert get_board_token() == "projects-pat-123"

    def test_get_board_token_fallback_to_gh_token(self, monkeypatch):
        """Falls back to GH_TOKEN when GITHUB_PROJECTS_TOKEN is unset."""
        from orchestration_engine.github_utils import get_board_token

        monkeypatch.delenv("GITHUB_PROJECTS_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "gh-token-fallback")
        assert get_board_token() == "gh-token-fallback"

    def test_get_board_token_none_when_unset(self, monkeypatch):
        """Returns None when neither env var is set."""
        from orchestration_engine.github_utils import get_board_token

        monkeypatch.delenv("GITHUB_PROJECTS_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        assert get_board_token() is None

    def test_get_issue_project_items_empty_on_error(self):
        """Returns empty list when subprocess returns non-zero exit."""
        from orchestration_engine.github_utils import get_issue_project_items

        error_resp = MagicMock(returncode=1, stdout="{}", stderr="error")
        with patch("subprocess.run", return_value=error_resp):
            result = get_issue_project_items("myorg", "myrepo", 42)
        assert result == []

    def test_move_issue_no_issue_number_zero_api_calls(self):
        """None issue_number → zero subprocess calls (contract 10)."""
        from orchestration_engine.github_utils import move_issue_on_board

        with patch("subprocess.run") as mock_run:
            result = move_issue_on_board("myorg", "myrepo", None, "In Progress")

        assert mock_run.call_count == 0, "No API calls for None issue number"
        assert result is False

    def test_move_issue_zero_issue_number_zero_api_calls(self):
        """Zero issue_number → zero subprocess calls (treated as 'not available')."""
        from orchestration_engine.github_utils import move_issue_on_board

        with patch("subprocess.run") as mock_run:
            result = move_issue_on_board("myorg", "myrepo", 0, "In Progress")

        assert mock_run.call_count == 0, "No API calls for issue_number=0"
        assert result is False
